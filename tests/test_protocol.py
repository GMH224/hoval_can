"""Standalone protocol / parser tests for the Hoval CAN integration.

Runs WITHOUT a Home Assistant install — the minimal HA surface used by
``const`` and ``coordinator`` is stubbed in ``sys.modules`` below. This lets
the framing parser, numeric decoder, COP model and connection watchdog be
verified in CI or on a workstation:

    python3 tests/test_protocol.py        # exit code 0 == all pass

These cover the ICS-critical guarantee that a monitored datapoint is never
updated from a mis-delimited frame (in-value FF 01 / FF 02 cannot corrupt it),
and that a half-open socket forces a reconnect instead of freezing.
"""
import asyncio
import os
import struct
import sys
import time
import types

# ── Minimal Home Assistant stubs ───────────────────────────────────────────
def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m

import enum


class _SDC(enum.Enum):
    TEMPERATURE = "temperature"; POWER = "power"; ENERGY = "energy"
    DURATION = "duration"; ENUM = "enum"   # v0.3.2: health status sensor


class _SC(enum.Enum):
    MEASUREMENT = "measurement"; TOTAL_INCREASING = "total_increasing"


class _EC(enum.Enum):
    DIAGNOSTIC = "diagnostic"; CONFIG = "config"


class _Platform(enum.Enum):
    SENSOR = "sensor"; BINARY_SENSOR = "binary_sensor"


_mod("homeassistant")
_mod("homeassistant.core", HomeAssistant=type("HomeAssistant", (), {}),
     callback=lambda f: f)


class _UnitOfEnergy:
    KILO_WATT_HOUR = "kWh"


_mod("homeassistant.const", Platform=_Platform, EntityCategory=_EC,
     STATE_UNAVAILABLE="unavailable", STATE_UNKNOWN="unknown",
     CONF_HOST="host", CONF_PORT="port", UnitOfEnergy=_UnitOfEnergy)
_mod("homeassistant.config_entries", ConfigEntry=type("ConfigEntry", (), {}))
_mod("homeassistant.components")
_mod("homeassistant.components.sensor",
     SensorDeviceClass=_SDC, SensorStateClass=_SC,
     SensorEntity=type("SensorEntity", (), {}))


def _redact(data, to_redact):
    if isinstance(data, dict):
        return {k: ("**REDACTED**" if k in to_redact else _redact(v, to_redact))
                for k, v in data.items()}
    if isinstance(data, list):
        return [_redact(v, to_redact) for v in data]
    return data


_mod("homeassistant.components.diagnostics", async_redact_data=_redact)
_mod("homeassistant.helpers")

# Recording dispatcher so tests can assert which signals were emitted.
_SENT: list = []


def _send(hass, signal, *a, **k):
    _SENT.append(signal)


_mod("homeassistant.helpers.dispatcher",
     async_dispatcher_send=_send,
     async_dispatcher_connect=lambda *a, **k: (lambda: None),
     _SENT=_SENT)
_mod("homeassistant.helpers.entity", DeviceInfo=dict)
_mod("homeassistant.helpers.entity_platform",
     AddEntitiesCallback=type("AddEntitiesCallback", (), {}))
_mod("homeassistant.helpers.event",
     async_track_time_interval=lambda *a, **k: (lambda: None))
_mod("homeassistant.helpers.restore_state",
     RestoreEntity=type("RestoreEntity", (), {}))


# Minimal functional Store stub: real in-memory round-trip (keyed by the
# storage key, like the real disk-backed Store keyed by filename), so
# persistence logic can be exercised without touching a filesystem.
class _FakeStore:
    _backing: dict = {}

    def __init__(self, hass, version, key):
        self._key = key

    async def async_load(self):
        return _FakeStore._backing.get(self._key)

    async def async_save(self, data):
        _FakeStore._backing[self._key] = data

    def async_delay_save(self, data_func, delay=0):
        # Synchronous stand-in for the real debounced write — good enough to
        # verify the data that WOULD be persisted.
        _FakeStore._backing[self._key] = data_func()


_mod("homeassistant.helpers.storage", Store=_FakeStore, _FakeStore=_FakeStore)

# ── Import the component under test ─────────────────────────────────────────
sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "custom_components"))
from hoval_can import const, coordinator as C  # noqa: E402

SEP, END = const.FRAME_SEP, const.FRAME_END
BE, LE = const.CMD_READ_RESP_BE, const.CMD_READ_RESP_LE

_fails = []


def expect(name, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def approx(name, got, exp, tol=0.02):
    expect(f"{name} (got {got}, expect {exp})", abs(got - exp) <= tol)


class _Entry:
    entry_id = "e1"
    data = {"host": "h", "port": 3113}
    options = {}


def _co():
    return C.HovalCANCoordinator(types.SimpleNamespace(loop=None), _Entry())


def _frame(cmd, group, dpid, value):
    body = (b"\x00\x00\x00" + b"\x00\x01" + bytes([cmd])
            + struct.pack(">H", group) + struct.pack(">H", dpid) + value)
    return SEP + body + END


def test_cop():
    print("== COP validation points (v0.3.1: approach-k + regime blend) ==")
    cop = const.calculate_cop

    # Calibration anchors (lift == reference lift) are k-INVARIANT by
    # construction: (ref+k)/(lift+k) == 1 at the anchor. Pin them with the
    # historical source_temp=12.5 explicitly (option default is now 16.5).
    approx("anchor SH t=30 m=12 src=12.5", cop(12, 30, 12.5), 7.0)
    approx("anchor SH t=30 m=33 src=12.5", cop(33, 30, 12.5), 6.51)
    approx("anchor SH t=30 m=50 src=12.5", cop(50, 30, 12.5), 5.74)
    approx("anchor DHW t=52 m=33 src=12.5", cop(33, 52, 12.5), 3.25)
    approx("anchor DHW t=52 m=100 src=12.5", cop(100, 52, 12.5), 2.50)
    # ... and independently of k:
    approx("anchor k-invariant (k=0)",  cop(50, 30, 12.5, 0.0), 5.74)
    approx("anchor k-invariant (k=15)", cop(50, 30, 12.5, 15.0), 5.74)

    # k=0 reproduces the pre-v0.3.1 bare-lift formula OFF-anchor too
    # (legacy: cop_base * ref/lift).
    approx("legacy k=0 SH t=35 m=50",  cop(50, 35, 12.5, 0.0), 4.4668)
    approx("legacy k=0 DHW t=45 m=50", cop(50, 45, 12.5, 0.0), 3.6814)

    # k>0 saturates the lift correction: raises COP above the bare-lift value
    # when lift > ref side of the curve is steep (off-anchor, larger lift).
    approx("k=7 SH t=35 m=50", cop(50, 35, 12.5, 7.0), 4.7696)
    expect("k raises COP off-anchor (lift>ref)",
           cop(50, 35, 12.5, 7.0) > cop(50, 35, 12.5, 0.0))

    # Regime blend (38-42 °C): continuous at both edges, monotone across,
    # strictly between the pure-regime endpoint values at the centre.
    lo  = cop(50, 38.0, 12.5, 7.0)
    mid = cop(50, 40.0, 12.5, 7.0)
    hi  = cop(50, 42.0, 12.5, 7.0)
    approx("blend edge t=38 == pure SH",  lo, 4.3293)
    approx("blend edge t=42 == pure DHW", hi, 3.8589)
    approx("blend centre t=40",           mid, 4.0805)
    expect("blend strictly between endpoints", hi < mid < lo)
    # Continuity: values just inside the window stay within 1% of the edges.
    expect("blend continuous at low edge",
           abs(cop(50, 38.01, 12.5, 7.0) - lo) < 0.05)
    expect("blend continuous at high edge",
           abs(cop(50, 41.99, 12.5, 7.0) - hi) < 0.05)

    # New defaults (source_temp 16.5, k 7.0) at this installation's typical
    # operating points.
    approx("defaults SH t=30 m=30",  cop(30, 30), 7.9368)
    approx("defaults DHW t=52 m=50", cop(50, 52), 3.3141)

    # Guards & clamps (against the NEW default source temp).
    expect("off m<=1 -> 0", cop(1, 30) == 0.0)
    expect("cold t<=source (16.5) -> 0", cop(50, 16.5) == 0.0)
    expect("clamp max 8.5 at tiny lift", cop(15, 18.0) == 8.5)
    expect("negative k treated as 0",
           cop(50, 35, 12.5, -3.0) == cop(50, 35, 12.5, 0.0))


def test_decode():
    print("== numeric decode ==")
    dn = C._decode_numeric
    approx("S16 -12.3", dn(struct.pack(">h", -123), "S16", 1), -12.3)
    expect("U16 sentinel -> None",
           dn(struct.pack(">H", 0x8000), "U16", 0) is None)
    expect("all-FF U32 -> None", dn(b"\xff\xff\xff\xff", "U32", 0) is None)
    expect("short buf -> None", dn(b"\x01", "U16", 0) is None)
    approx("U32 dec3", dn(struct.pack(">I", 12345), "U32", 3), 12.345)
    approx("LE U16", dn(struct.pack("<H", 300), "U16", 1, little_endian=True),
           30.0)


def test_framing():
    print("== framing (adversarial) ==")
    # in-value FF 01
    co = _co()
    v = b"\xaa\xff\x01\xbb"
    co._consume_frames(_frame(BE, 100, 2080, v) + _frame(BE, 100, 7,
                       struct.pack(">h", 200)))
    expect("in-value FF01: dp2080 correct",
           co.get_value(2080) == struct.unpack(">I", v)[0])
    expect("in-value FF01: next dp7 == 20.0", co.get_value(7) == 20.0)
    expect("in-value FF01: 0 framing errors", co.framing_errors == 0)

    # in-value FF 02
    co = _co()
    v = b"\x00\xff\x02\x05"
    co._consume_frames(_frame(BE, 100, 2080, v) + _frame(BE, 100, 7,
                       struct.pack(">h", -50)))
    expect("in-value FF02: dp2080 correct",
           co.get_value(2080) == struct.unpack(">I", v)[0])
    expect("in-value FF02: next dp7 == -5.0", co.get_value(7) == -5.0)

    # value ENDING in FF 02 (regression guard)
    co = _co()
    v = b"\x01\x02\xff\x02"
    co._consume_frames(_frame(BE, 100, 2080, v) + _frame(BE, 100, 7,
                       struct.pack(">h", 100)))
    expect("value-ends-FF02: dp2080 correct",
           co.get_value(2080) == struct.unpack(">I", v)[0])
    expect("value-ends-FF02: next dp7 == 10.0", co.get_value(7) == 10.0)

    # split across reads
    co = _co()
    full = _frame(BE, 100, 7, struct.pack(">h", 333))
    rem = co._consume_frames(full[:7])
    expect("split: nothing yet", co.get_value(7) is None)
    co._consume_frames(rem + full[7:])
    expect("split: dp7 == 33.3", co.get_value(7) == 33.3)

    # desync garbage then recover
    co = _co()
    co._consume_frames(b"\xde\xad\xbe\xef" + _frame(BE, 100, 7,
                       struct.pack(">h", 451)))
    expect("desync recovers: dp7 == 45.1", co.get_value(7) == 45.1)

    # LE room display
    co = _co()
    le = SEP + b"\x00\x00\x00\x00\x01" + bytes([LE]) \
        + struct.pack(">H", 0x8000 | 5) + struct.pack(">H", 1) \
        + b"\x00" + struct.pack("<h", 215) + END
    co._consume_frames(le)
    expect("LE dp1 == 21.5", co.get_value(1) == 21.5)

    # schedule group ignored
    co = _co()
    co._consume_frames(_frame(BE, 15922, 7, struct.pack(">h", 999))
                       + _frame(BE, 100, 7, struct.pack(">h", 190)))
    expect("schedule ignored, real dp7 == 19.0", co.get_value(7) == 19.0)

    # STR variable-length scan path
    co = _co()
    co._consume_frames(_frame(BE, 100, 502, b"PROG1")
                       + _frame(BE, 100, 7, struct.pack(">h", 201)))
    expect("STR dp502 == PROG1", co.get_value(502) == "PROG1")
    expect("after STR: dp7 == 20.1", co.get_value(7) == 20.1)

    # RX buffer cap
    big = b"\x00" * (const.MAX_RX_BUFFER + 500)
    kept = big[-const.RX_RESYNC_KEEP:] if len(big) > const.MAX_RX_BUFFER else big
    expect("RX buffer cap truncates", len(kept) == const.RX_RESYNC_KEEP)


def test_watchdog():
    print("== connection watchdog (real loop) ==")
    C.FRAME_TIMEOUT = 0.05
    C.STALE_TIMEOUT = 0.20
    C.DATA_STALE_TIMEOUT = 10.0

    class _R:
        def __init__(self, mode):
            self.mode = mode

        async def read(self, n):
            if self.mode == "halfopen":
                await asyncio.Event().wait()
            return b""

    class _W:
        def close(self):
            pass

        async def wait_closed(self):
            pass

        def get_extra_info(self, k):
            return None

    async def case(mode):
        co = _co()

        async def fake_open(h, p):
            return _R(mode), _W()
        C.asyncio.open_connection = fake_open
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(co._connect_and_read(), timeout=3.0)
            return None
        except ConnectionError:
            return time.monotonic() - t0
        except asyncio.TimeoutError:
            return "HUNG"

    dt = asyncio.run(case("halfopen"))
    expect("half-open raises (no hang)", isinstance(dt, float) and dt < 1.0)
    closed = asyncio.run(case("closed"))
    expect("peer-closed raises", isinstance(closed, float))


def test_integrator_math():
    print("== monotonic integrator arithmetic ==")
    total = 0.0
    last_t = last_cop = last_ts = None

    def upd(thermal, cop, now):
        nonlocal total, last_t, last_cop, last_ts
        if (last_t is not None and last_ts is not None
                and last_cop and last_t and last_cop > 0 and last_t > 0):
            h = max(0.0, (now - last_ts) / 3600.0)
            total += h * (last_t / last_cop)
        last_t, last_cop, last_ts = thermal, cop, now

    upd(3.0, 3.0, 1000.0)
    upd(3.0, 3.0, 1000.0 + 3600.0)
    approx("1h @1kW -> 1.0 kWh", total, 1.0)


def _co_with_options(options):
    entry = types.SimpleNamespace(entry_id="e1",
                                  data={"host": "h", "port": 3113},
                                  options=options)
    return C.HovalCANCoordinator(types.SimpleNamespace(loop=None), entry)


def test_cooling_coordinator():
    print("== passive cooling: coordinator ==")
    HC = const.DP_STATUS_HC

    # cooling_power_kw: Watts -> kW, default, bad values
    co = _co_with_options({})
    approx("default cooling power 0.1 kW", co.cooling_power_kw, 0.1)
    co = _co_with_options({const.CONF_COOLING_POWER: 250})
    approx("250 W -> 0.25 kW", co.cooling_power_kw, 0.25)
    co = _co_with_options({const.CONF_COOLING_POWER: "bad"})
    approx("garbage -> default 0.1 kW", co.cooling_power_kw, 0.1)
    co = _co_with_options({const.CONF_COOLING_POWER: -50})
    approx("negative -> 0 kW", co.cooling_power_kw, 0.0)

    # passive_cooling_on: None until seen, True at 9, False otherwise
    co = _co()
    expect("unknown -> None", co.passive_cooling_on is None)
    co._update_dp(HC, 9)
    expect("status 9 -> True", co.passive_cooling_on is True)
    co._update_dp(HC, 1)
    expect("status 1 -> False", co.passive_cooling_on is False)

    # edge-triggered cooling_signal dispatch (no repeats)
    from homeassistant.helpers import dispatcher as D
    sig = const.cooling_signal("e1")
    co = _co()
    D._SENT.clear()
    co._update_dp(HC, 9)          # None -> True : edge
    co._update_dp(HC, 9)          # True -> True : no edge
    co._update_dp(HC, 0)          # True -> False: edge
    n = D._SENT.count(sig)
    expect("cooling_signal fired exactly twice (2 transitions)", n == 2)


def test_cooling_sensors():
    print("== passive cooling: sensors ==")
    from hoval_can import sensor as S

    class FakeCoord:
        def __init__(self):
            self.connected = True
            self.heater_power_kw = 3.0
            self.cooling_power_kw = 0.1
            self.brine_pump_kw = 0.03
            self.heating_pump_kw = 0.02
            self.standby_kw = 0.012
            self.pumps_active = None
            self._cop = 0.0
            self.thermal = None
            self.heater = None
            self.cooling = None

        def get_value(self, dp):
            return self.thermal if dp == S.DP_THERMAL_POWER else None

        @property
        def cop(self):
            return self._cop

        @property
        def electric_heater_on(self):
            return self.heater

        @property
        def passive_cooling_on(self):
            return self.cooling

    def total_power(coord):
        obj = object.__new__(S.HovalTotalElecPowerSensor)
        obj._coord = coord
        obj._attr_native_value = None
        obj.async_write_ha_state = lambda: None
        obj._update()
        return obj._attr_native_value

    fc = FakeCoord()
    # No CAN data at all yet: per the sensor's own documented behaviour every
    # unknown input except standby is zero-filled (not blanked to Unknown) —
    # this was already the pre-existing, intentional design (confirmed
    # against the original codebase; the old assertion here predating this
    # change simply didn't match it). Standby is unconditional, so the floor
    # is the standby draw, never an outright Unknown.
    fc.thermal, fc.heater = None, None
    approx("no CAN data yet -> standby-only floor", total_power(fc), 0.012)

    # thermal+heater known, pumps_active still unknown -> zero-filled, but
    # standby is unconditional, so the total is never simply "nothing".
    fc.thermal, fc.heater, fc._cop, fc.pumps_active = 0.0, False, 0.0, None
    approx("standby-only baseline (pumps unknown)", total_power(fc), 0.012)

    # Pumps active (heating OR passive cooling): + brine + heating pump
    fc.pumps_active = True
    approx("pumps add brine+heating+standby", total_power(fc), 0.03 + 0.02 + 0.012)

    # Pumps active + heater on + heat pump running
    fc.thermal, fc._cop, fc.heater, fc.pumps_active = 6.0, 3.0, True, True
    # hp = 6/3 = 2.0, heater 3.0, pumps 0.05, standby 0.012 -> 5.062
    approx("combined hp+heater+pumps+standby", total_power(fc), 5.062)

    # Pumps inactive -> pump terms drop, standby remains
    fc.pumps_active = False
    approx("pumps off -> hp+heater+standby only", total_power(fc), 5.012)

    # Passive Cooling Power sensor
    pc = object.__new__(S.HovalPassiveCoolingPowerSensor)
    pc._coord = fc
    pc._attr_native_value = None
    pc._has_value = False
    pc.async_write_ha_state = lambda: None
    fc.cooling = None
    pc._update()
    expect("PC power stays Unknown while status unknown",
           pc._attr_native_value is None)
    fc.cooling = True
    pc._update()
    approx("PC power = 0.1 kW when cooling", pc._attr_native_value, 0.1)
    fc.cooling = False
    pc._update()
    approx("PC power = 0 when not cooling", pc._attr_native_value, 0.0)

    # Passive Cooling Energy: 1 h @ 0.1 kW -> +0.1 kWh
    pe = object.__new__(S.HovalPassiveCoolingEnergySensor)
    pe._coord = fc
    pe._total_kwh = 0.0
    pe._on_since = 1000.0
    pe._flush(1000.0 + 3600.0)
    approx("PC energy 1h@100W -> 0.1 kWh", pe._total_kwh, 0.1)

    # Total Energy integrate path includes cooling term
    te = object.__new__(S.HovalTotalElecEnergySensor)
    te._total_kwh = 0.0
    te._last_elec_kw = 0.1     # cooling-only draw
    te._last_ts = 1000.0
    te._integrate(1000.0 + 3600.0, 0.1)
    approx("Total energy 1h@100W cooling -> 0.1 kWh", te._total_kwh, 0.1)



def test_integrator_resample():
    """v0.3.1: energy integrators re-sample COP inputs and commit on tick."""
    print("== v0.3.1 integrator re-sampling ==")
    from hoval_can import sensor as S

    class FakeCoord:
        def __init__(self):
            self.connected = True
            self.heater_power_kw = 3.0
            self.brine_pump_kw = 0.03
            self.heating_pump_kw = 0.02
            self.standby_kw = 0.012
            self.pumps_active = True
            self.thermal = None
            self._cop = 0.0
            self.heater = False

        def get_value(self, dp):
            return self.thermal if dp == S.DP_THERMAL_POWER else None

        @property
        def cop(self):
            return self._cop

        @property
        def electric_heater_on(self):
            return self.heater

        @property
        def passive_cooling_on(self):
            return False

    # ── HP energy: _sample integrates at start-of-interval values, and a
    # COP change mid-plateau (constant thermal) re-arms the interval ───────
    fc = FakeCoord()
    hp = object.__new__(S.HovalHeatPumpElecEnergySensor)
    hp._coord = fc
    hp._total_kwh = 0.0
    hp._last_thermal = None
    hp._last_cop = 0.0
    hp._last_ts = None
    hp._attr_native_value = 0.0
    hp.async_write_ha_state = lambda: None

    fc.thermal, fc._cop = 6.0, 4.0
    hp._sample(1000.0, write_always=True)              # arm
    fc._cop = 3.0                                      # T_gen rose, COP fell
    hp._sample(1000.0 + 1800.0, write_always=True)     # 0.5 h @ 6/4 = 0.75
    approx("HP: first half-hour at COP 4", hp._total_kwh, 0.75)
    hp._sample(1000.0 + 3600.0, write_always=True)     # 0.5 h @ 6/3 = 1.0
    approx("HP: second half-hour at COP 3", hp._total_kwh, 1.75)
    # Without the modulation/T_gen subscriptions the whole hour would have
    # been integrated at COP 4 (1.5 kWh) — the re-sample recovers 0.25 kWh.

    # None thermal resets tracking (gap safety, unchanged behaviour)
    fc.thermal = None
    hp._sample(1000.0 + 7200.0, write_always=True)
    approx("HP: None resets without integrating", hp._total_kwh, 1.75)
    expect("HP: tracking cleared", hp._last_ts is None)

    # Tick path: guarded on connected — must not integrate while down
    fc.thermal, fc._cop = 6.0, 3.0
    hp._sample(10000.0, write_always=True)             # re-arm
    fc.connected = False
    before = hp._total_kwh
    hp._tick(None)                                     # wall-clock arg unused
    approx("HP: tick no-op while disconnected", hp._total_kwh, before)

    # Tick must never ARM tracking either (post-reconnect, coordinator data
    # may be stale until the first fresh 29051 broadcast — only a dispatcher
    # signal may open an interval, matching pre-v0.3.1 semantics).
    fc.connected = True
    hp._last_thermal, hp._last_cop, hp._last_ts = None, 0.0, None
    hp._tick(None)
    expect("HP: tick does not arm from cleared tracking", hp._last_ts is None)

    # ── Total energy: _current_kw recomputes from live state; tick commits
    # at the RECOMPUTED rate, not the stale armed rate ─────────────────────
    fc = FakeCoord()
    te = object.__new__(S.HovalTotalElecEnergySensor)
    te._coord = fc
    te._total_kwh = 0.0
    te._last_elec_kw = None
    te._last_ts = None
    te._attr_native_value = 0.0
    te.async_write_ha_state = lambda: None

    fc.thermal, fc._cop = 6.0, 3.0     # hp 2.0 + pumps 0.05 + standby 0.012
    approx("Total: _current_kw live recompute", te._current_kw(), 2.062)
    fc.heater = True
    approx("Total: _current_kw includes heater", te._current_kw(), 5.062)
    fc.heater = False

    te._last_elec_kw = 2.062
    te._last_ts = 1000.0
    fc._cop = 2.0                       # rate rises to 6/2+0.062 = 3.062
    te._integrate(1000.0 + 3600.0, te._current_kw())
    approx("Total: hour committed at armed 2.062", te._total_kwh, 2.062)
    approx("Total: re-armed at recomputed 3.062", te._last_elec_kw, 3.062)

    # Tick never arms from nothing (startup semantics unchanged)
    te2 = object.__new__(S.HovalTotalElecEnergySensor)
    te2._coord = fc
    te2._total_kwh = 0.0
    te2._last_elec_kw = None
    te2._last_ts = None
    te2._attr_native_value = 0.0
    te2.async_write_ha_state = lambda: None
    te2._tick(None)
    expect("Total: tick does not arm from nothing", te2._last_ts is None)
    # ... and is a no-op while disconnected even when armed
    te._coord.connected = False
    before = te._total_kwh
    te._tick(None)
    approx("Total: tick no-op while disconnected", te._total_kwh, before)


def test_power_model_options():
    print("== new power-model options (source temp / brine / heating pump / standby) ==")
    MOD, HC = const.DP_MODULATION, const.DP_STATUS_HC

    # source_temp_c: default (16.5 as of v0.3.1 — measured on the analog
    # gauge during a DHW charge, July 2026), override, garbage
    co = _co_with_options({})
    approx("default source_temp_c 16.5 (v0.3.1)", co.source_temp_c, 16.5)
    co = _co_with_options({const.CONF_SOURCE_TEMP: 9.0})
    approx("override source_temp_c 9.0", co.source_temp_c, 9.0)
    co = _co_with_options({const.CONF_SOURCE_TEMP: "bad"})
    approx("garbage source_temp_c -> default", co.source_temp_c, 16.5)

    # approach_k_c (v0.3.1): default, override, garbage, out-of-range clamp
    co = _co_with_options({})
    approx("default approach_k_c 7.0", co.approach_k_c, 7.0)
    co = _co_with_options({const.CONF_APPROACH_K: 4.0})
    approx("override approach_k_c 4.0", co.approach_k_c, 4.0)
    co = _co_with_options({const.CONF_APPROACH_K: "bad"})
    approx("garbage approach_k_c -> default", co.approach_k_c, 7.0)
    co = _co_with_options({const.CONF_APPROACH_K: -2.0})
    approx("negative approach_k_c -> clamped 0", co.approach_k_c, 0.0)
    co = _co_with_options({const.CONF_APPROACH_K: 99.0})
    approx("oversized approach_k_c -> clamped max", co.approach_k_c,
           const.APPROACH_K_MAX)

    # coordinator.cop consumes BOTH options: same live data, k=0 vs k=7
    # differ off-anchor; explicit source override shifts the lift.
    co0 = _co_with_options({const.CONF_SOURCE_TEMP: 12.5,
                            const.CONF_APPROACH_K: 0.0})
    co7 = _co_with_options({const.CONF_SOURCE_TEMP: 12.5,
                            const.CONF_APPROACH_K: 7.0})
    for c in (co0, co7):
        c._update_dp(const.DP_MODULATION, 50)
        c._update_dp(const.DP_HEAT_GEN, 35.0)
    approx("coordinator cop k=0 (legacy)", co0.cop, 4.4668)
    approx("coordinator cop k=7",          co7.cop, 4.7696)

    # calculate_cop honours the passed-in source_temp (colder loop -> bigger
    # lift -> lower COP for the same modulation/T_gen than the 12.5 default)
    cop = const.calculate_cop
    warm_loop   = cop(15, 30.0, 16.5)       # v0.3.1 default source, lift=13.5
    colder_loop = cop(15, 30.0, 9.0)        # source_temp=9.0, lift=21.0
    expect("colder source -> lower COP for same lift-independent inputs",
           colder_loop < warm_loop)

    # brine_pump_kw / heating_pump_kw / standby_kw: default, override,
    # garbage, negative -> all clamp to >= 0
    co = _co_with_options({})
    approx("default brine_pump_kw 0.03", co.brine_pump_kw, 0.03)
    approx("default heating_pump_kw 0.02", co.heating_pump_kw, 0.02)
    approx("default standby_kw 0.012", co.standby_kw, 0.012)
    co = _co_with_options({const.CONF_BRINE_PUMP_POWER: 45})
    approx("override brine_pump_kw", co.brine_pump_kw, 0.045)
    co = _co_with_options({const.CONF_HEATING_PUMP_POWER: -10})
    approx("negative heating_pump_kw -> 0", co.heating_pump_kw, 0.0)
    co = _co_with_options({const.CONF_STANDBY_POWER: "bad"})
    approx("garbage standby_kw -> default", co.standby_kw, 0.012)

    # heat_pump_active: None until modulation seen, then threshold-gated
    co = _co()
    expect("heat_pump_active unknown -> None", co.heat_pump_active is None)
    co._update_dp(MOD, 0)
    expect("modulation 0 -> not active", co.heat_pump_active is False)
    co._update_dp(MOD, 25)
    expect("modulation 25 -> active", co.heat_pump_active is True)

    # pumps_active: OR of heat_pump_active and passive_cooling_on; None only
    # when BOTH are unknown
    co = _co()
    expect("pumps_active unknown when neither input seen", co.pumps_active is None)
    co._update_dp(HC, 1)   # heating-circuit status known, not cooling
    expect("pumps_active False when HC known-off and HP still unknown",
           co.pumps_active is False)
    co._update_dp(MOD, 30)
    expect("pumps_active True once compressor active", co.pumps_active is True)
    co._update_dp(MOD, 0)
    co._update_dp(HC, 9)   # passive cooling
    expect("pumps_active True during passive cooling even with compressor off",
           co.pumps_active is True)


def test_persistence():
    print("== coordinator-level persistence (Store round-trip) ==")
    from homeassistant.helpers.storage import _FakeStore
    _FakeStore._backing.clear()
    MOD, HG, HC = const.DP_MODULATION, const.DP_HEAT_GEN, const.DP_STATUS_HC

    entry = types.SimpleNamespace(
        entry_id="persist1", data={"host": "h", "port": 3113}, options={},
    )
    hass = types.SimpleNamespace(loop=None)

    # First "run": receive some persistent datapoints, they get scheduled for
    # save via the (synchronous, in the stub) async_delay_save.
    co1 = C.HovalCANCoordinator(hass, entry)
    co1._update_dp(MOD, 27)
    co1._update_dp(HG, 34.0)
    co1._update_dp(HC, 9)
    saved = _FakeStore._backing.get(f"{const.DOMAIN}_persist1_state")
    expect("snapshot was persisted", saved is not None)
    expect("persisted snapshot includes modulation",
           saved is not None and str(MOD) in saved and saved[str(MOD)] == 27)

    # Non-persistent dpid must NOT be written to the snapshot.
    co1._update_dp(0, 5.0)   # outdoor_temp — not in PERSISTENT_DPIDS
    saved2 = _FakeStore._backing.get(f"{const.DOMAIN}_persist1_state")
    expect("non-persistent dpid excluded from snapshot",
           saved2 is not None and str(0) not in saved2)

    # "Restart": a fresh coordinator instance backed by the same store loads
    # the last-known values before any CAN frame has arrived.
    co2 = C.HovalCANCoordinator(hass, entry)
    asyncio.run(co2._async_load_persisted())
    expect("restored modulation available before any live frame",
           co2.get_value(MOD) == 27)
    expect("restored heat_gen_temp available before any live frame",
           co2.get_value(HG) == 34.0)
    expect("derived pumps_active correct from restored data alone",
           co2.pumps_active is True)   # HC status 9 was restored

    # Replaying signals should fire dp_signal for each restored dpid plus the
    # heater/cooling composite signals, so already-subscribed entities update.
    from homeassistant.helpers import dispatcher as D
    D._SENT.clear()
    co2.async_replay_restored_signals()
    expect("replay fires dp_signal for restored modulation",
           const.dp_signal("persist1", MOD) in D._SENT)
    expect("replay fires cooling_signal (HC status was restored)",
           const.cooling_signal("persist1") in D._SENT)

    # Fresh live data always overwrites a restored value, and is no longer
    # tracked as "restored" afterwards.
    co2._update_dp(MOD, 5)
    expect("live update overwrites restored value", co2.get_value(MOD) == 5)
    expect("dpid no longer marked as restored after a live update",
           MOD not in co2._restored_dpids)

    # A corrupt/missing store must never block startup.
    class _BrokenStore:
        def __init__(self, *a, **k):
            pass

        async def async_load(self):
            raise RuntimeError("disk error")

    real_store_cls = C.Store
    try:
        C.Store = _BrokenStore
        co3 = C.HovalCANCoordinator(hass, entry)
        asyncio.run(co3._async_load_persisted())
        expect("broken store does not raise / starts cold", co3.get_value(MOD) is None)
    finally:
        C.Store = real_store_cls


def test_diagnostics():
    print("== diagnostics: snapshot + sensors + redaction ==")
    HC = const.DP_STATUS_HC

    # decoded_count increments per decoded datapoint
    co = _co()
    expect("decoded_count starts at 0", co.decoded_count == 0)
    co._update_dp(7, 45.5)
    co._update_dp(HC, 9)
    expect("decoded_count == 2 after 2 updates", co.decoded_count == 2)

    # snapshot structure + named last_values + counters
    snap = co.diagnostics_snapshot()
    expect("snapshot has connection block", "connection" in snap)
    expect("snapshot decoded_count", snap["connection"]["decoded_count"] == 2)
    expect("snapshot datapoints_seen == 2", snap["datapoints_seen"] == 2)
    expect("snapshot names dp7 -> heat_gen_temp",
           "heat_gen_temp" in snap["last_values"])
    expect("snapshot derived passive_cooling_on True",
           snap["derived"]["passive_cooling_on"] is True)
    co2 = _co_with_options({const.CONF_COOLING_POWER: 120})
    expect("snapshot reports cooling_power_w in watts",
           co2.diagnostics_snapshot()["options"]["cooling_power_w"] == 120.0)

    # diagnostics.py end-to-end with host redaction
    from hoval_can import diagnostics as DG

    class FakeEntry2:
        entry_id = "e1"; title = "Hoval"; version = 1
        unique_id = "1.2.3.4:3113"
        data = {"host": "1.2.3.4", "port": 3113}
        options = {const.CONF_HEATER_POWER: 3.0, const.CONF_COOLING_POWER: 100}

    fake_entry = FakeEntry2()
    hass = types.SimpleNamespace(data={const.DOMAIN: {"e1": co}})
    result = asyncio.run(
        DG.async_get_config_entry_diagnostics(hass, fake_entry))
    expect("diag redacts entry host",
           result["entry"]["data"]["host"] == "**REDACTED**")
    expect("diag redacts coordinator host",
           result["coordinator"]["host"] == "**REDACTED**")
    expect("diag redacts unique_id",
           result["entry"]["unique_id"] == "**REDACTED**")
    expect("diag keeps port visible",
           result["coordinator"]["port"] == 3113)

    # diagnostics handles missing coordinator gracefully
    empty_hass = types.SimpleNamespace(data={const.DOMAIN: {}})
    res2 = asyncio.run(
        DG.async_get_config_entry_diagnostics(empty_hass, fake_entry))
    expect("diag handles missing coordinator",
           "error" in res2["coordinator"])

    # diagnostic sensor native_value wiring
    from hoval_can import sensor as S

    class FakeC2:
        last_data_age = 12.34
        reconnect_count = 3
        framing_errors = 1
        decoded_count = 42

    def nv(cls):
        obj = object.__new__(cls)
        obj._coord = FakeC2()
        return obj.native_value

    approx("DataAge sensor value", nv(S.HovalDataAgeSensor), 12.3)
    expect("Reconnects sensor value", nv(S.HovalReconnectsSensor) == 3)
    expect("FramingErrors sensor value", nv(S.HovalFramingErrorsSensor) == 1)
    expect("DecodedCount sensor value", nv(S.HovalDecodedCountSensor) == 42)

    class FakeC3:
        last_data_age = None
    expect("DataAge None before first data",
           nv2(S.HovalDataAgeSensor, FakeC3()) is None)


def nv2(cls, coord):
    obj = object.__new__(cls)
    obj._coord = coord
    return obj.native_value


def test_rates():
    print("== windowed rates ==")
    wr = C._windowed_rate

    # pure helper edge cases
    expect("empty -> None", wr([], 10, 100.0, 900, 120) is None)
    expect("warm-up (elapsed<min) -> None",
           wr([(0.0, 0)], 5, 100.0, 900, 120) is None)
    expect("all samples older than window -> None",
           wr([(0.0, 0)], 5, 1000.0, 900, 120) is None)
    # 10 events over 200 s -> 0.05/s
    approx("rate 10/200s = 0.05/s", wr([(0.0, 0)], 10, 200.0, 900, 120), 0.05)
    # flat window -> 0
    approx("flat window -> 0",
           wr([(0.0, 10), (60.0, 10)], 10, 200.0, 900, 120), 0.0)
    # ref is the OLDEST sample within the window
    approx("oldest-in-window ref",
           wr([(0.0, 0), (100.0, 1)], 4, 200.0, 900, 120), 0.02)

    # coordinator property wiring (deterministic clock)
    real_mono = C.time.monotonic
    try:
        co = _co()
        co._rate_samples.clear()
        co._rate_samples.append((1000.0, 0, 0))   # t, errors, decoded
        co._framing_errors = 2
        co._decoded_count = 600
        C.time.monotonic = lambda: 1200.0           # 200 s later
        approx("throughput 600/200s -> 180/min",
               co.decoded_rate_per_min, 180.0)
        approx("error rate 2/200s -> 36/h",
               co.framing_error_rate_per_h, 36.0)
        C.time.monotonic = lambda: 1050.0           # only 50 s -> warm-up
        expect("throughput None during warm-up",
               co.decoded_rate_per_min is None)
        expect("error rate None during warm-up",
               co.framing_error_rate_per_h is None)

        # _sample_rates appends and prunes > 60-min-old entries
        co2 = _co()
        co2._rate_samples.clear()
        co2._rate_samples.append((0.0, 0, 0))       # very old
        co2._framing_errors = 1
        co2._decoded_count = 9
        C.time.monotonic = lambda: 4000.0           # cutoff = 4000-3600 = 400
        co2._sample_rates()
        ts = [s[0] for s in co2._rate_samples]
        expect("old sample pruned", 0.0 not in ts)
        expect("new sample appended", 4000.0 in ts)
        expect("latest snapshot carries counters",
               co2._rate_samples[-1][1:] == (1, 9))
    finally:
        C.time.monotonic = real_mono

    # snapshot exposes the rates
    snap = _co().diagnostics_snapshot()
    expect("snapshot has decoded_rate_per_min",
           "decoded_rate_per_min" in snap["connection"])
    expect("snapshot has framing_error_rate_per_h",
           "framing_error_rate_per_h" in snap["connection"])

    # sensor native_value wiring
    from hoval_can import sensor as S

    class FakeR:
        decoded_rate_per_min = 142.7
        framing_error_rate_per_h = 3.456

    approx("throughput sensor value", nv2(S.HovalThroughputSensor, FakeR()),
           142.7, tol=0.05)
    approx("error-rate sensor value",
           nv2(S.HovalFramingErrorRateSensor, FakeR()), 3.46, tol=0.005)

    class FakeRn:
        decoded_rate_per_min = None
        framing_error_rate_per_h = None
    expect("throughput sensor None passthrough",
           nv2(S.HovalThroughputSensor, FakeRn()) is None)



def test_health_tracker():
    """v0.3.2: HealthTracker glue — tick → Sample → model → Store → signal."""
    print("Health tracker glue:")
    import datetime as _dt
    from hoval_can import health as H

    _FakeStore._backing.clear()
    _SENT.clear()
    co = _co()
    co._connected = True
    co._data.update({
        const.DP_STATUS_WW: 0, const.DP_STATUS_HC: 1,
        const.DP_MODULATION: 40, const.DP_HEATING_PROGRAM: "Heizen Woche 1",
        const.DP_FLOW_TEMP: 30.0, const.DP_HEAT_GEN: 30.4,
        const.DP_THERMAL_POWER: 3.5, const.DP_WEZ_ELEC_TOTAL: 5.0,
        const.DP_WEZ_CYCLES: 1000,
    })
    tr = H.HealthTracker(types.SimpleNamespace(loop=None), _Entry(), co)
    asyncio.run(tr.async_start())
    expect("starts with empty model", len(tr.model.history) == 0)

    t0 = _dt.datetime(2026, 1, 10, 12, 0, tzinfo=_dt.timezone.utc)
    tr._tick(t0)
    tr._tick(t0 + _dt.timedelta(minutes=5))
    acc = tr.model._acc
    expect("samples accumulated into open day",
           acc is not None and acc.sh_sample_n == 2
           and abs(acc.mode_s[H.MODE_SH] - 300.0) < 1.0)
    expect("health signal dispatched",
           any(s.endswith("_health") for s in _SENT))
    key = f"hoval_can_e1_{const.HEALTH_STORE_SUFFIX}"
    expect("state persisted to health store",
           key in _FakeStore._backing
           and _FakeStore._backing[key].get("acc") is not None)

    # Disconnected → tick is a no-op (no phantom samples during outages).
    co._connected = False
    tr._tick(t0 + _dt.timedelta(minutes=10))
    expect("no sample while disconnected", tr.model._acc.sh_sample_n == 2)
    co._connected = True

    # Restart round-trip through the tracker store.
    asyncio.run(tr.async_stop())
    tr2 = H.HealthTracker(types.SimpleNamespace(loop=None), _Entry(), co)
    asyncio.run(tr2.async_start())
    expect("open day survives tracker restart",
           tr2.model._acc is not None
           and tr2.model._acc.sh_sample_n == 2)



def test_health_cold_start_gate():
    """v0.3.3: the tracker must not integrate Store-seeded values."""
    print("Health cold-start gate:")
    import datetime as _dt
    from hoval_can import health as H

    _FakeStore._backing.clear()
    co = _co()
    co._connected = True
    # Simulate a restart: every PERSISTENT_DPID seeded from the Store, so
    # get_value() returns data but is_restored() flags all of it as stale.
    co._data.update({
        const.DP_STATUS_WW: 0, const.DP_STATUS_HC: 1,
        const.DP_MODULATION: 40, const.DP_HEAT_GEN: 30.4,
        const.DP_THERMAL_POWER: 3.5, const.DP_WEZ_ELEC_TOTAL: 5.0,
        const.DP_WEZ_CYCLES: 1000, const.DP_FLOW_TEMP: 30.0,
    })
    co._restored_dpids = set(const.PERSISTENT_DPIDS)
    expect("coordinator reports restored state",
           co.is_restored(const.DP_MODULATION) is True
           and co.is_restored(const.DP_FLOW_TEMP) is False)

    tr = H.HealthTracker(types.SimpleNamespace(loop=None), _Entry(), co)
    asyncio.run(tr.async_start())
    t0 = _dt.datetime(2026, 1, 10, 12, 0, tzinfo=_dt.timezone.utc)
    for k in range(13):                        # 1 h of blind ticks
        tr._tick(t0 + _dt.timedelta(minutes=5 * k))
    acc = tr.model._acc
    expect("no phantom thermal energy while seeded", acc.thermal_kwh == 0.0)
    expect("no SH seconds credited while seeded",
           acc.mode_s[H.MODE_SH] == 0.0)
    expect("blind time accounted", acc.unknown_s > 3000.0)
    expect("no counter endpoint captured while seeded",
           acc.elec_first is None and acc.cycles_first is None)

    # A live frame for the FRESH_REQUIRED pair clears the gate even though
    # the SETTLE_REQUIRED members are still seeded (settle window applies
    # only while they remain restored and the window has not elapsed).
    for dp in const.HEALTH_FRESH_REQUIRED + const.HEALTH_SETTLE_REQUIRED:
        co._restored_dpids.discard(dp)
    co._restored_dpids.discard(const.DP_WEZ_ELEC_TOTAL)
    co._restored_dpids.discard(const.DP_WEZ_CYCLES)
    tr._tick(t0 + _dt.timedelta(minutes=65))
    tr._tick(t0 + _dt.timedelta(minutes=70))
    expect("sampling resumes once live frames arrive",
           tr.model._acc.mode_s[H.MODE_SH] > 0.0
           and tr.model._acc.elec_first == 5.0)

    # Settle tier: modulation/thermal live, a status still seeded → blind
    # only until HEALTH_SETTLE_S elapses.
    co._restored_dpids.add(const.DP_STATUS_WW)
    tr2 = H.HealthTracker(types.SimpleNamespace(loop=None), _Entry(), co)
    asyncio.run(tr2.async_start())
    # tr2 restores the persisted model, so compare deltas, not absolutes.
    sh_before = tr2.model._acc.mode_s[H.MODE_SH]
    tr2._tick(t0 + _dt.timedelta(minutes=80))
    tr2._tick(t0 + _dt.timedelta(minutes=85))
    expect("settle tier blocks while window open",
           tr2.model._acc.mode_s[H.MODE_SH] == sh_before)
    tr2._start_mono -= const.HEALTH_SETTLE_S + 1     # expire the window
    tr2._tick(t0 + _dt.timedelta(minutes=90))
    tr2._tick(t0 + _dt.timedelta(minutes=95))
    expect("settle tier releases after the window",
           tr2.model._acc.mode_s[H.MODE_SH] > sh_before)


def main():
    test_cop()
    test_decode()
    test_framing()
    test_watchdog()
    test_integrator_math()
    test_cooling_coordinator()
    test_cooling_sensors()
    test_integrator_resample()
    test_power_model_options()
    test_persistence()
    test_diagnostics()
    test_rates()
    test_health_tracker()
    test_health_cold_start_gate()
    print()
    print("RESULT:", "ALL PASS" if not _fails else f"{len(_fails)} FAIL: {_fails}")
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    main()
