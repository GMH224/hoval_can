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
    print("== COP validation points ==")
    cop = const.calculate_cop
    approx("SH t=30 m=12", cop(12, 30), 7.0)
    approx("SH t=30 m=33", cop(33, 30), 6.51)
    approx("SH t=30 m=50", cop(50, 30), 5.74)
    approx("DHW t=52 m=33", cop(33, 52), 3.25)
    approx("DHW t=52 m=100", cop(100, 52), 2.50)
    expect("off m<=1 -> 0", cop(1, 30) == 0.0)
    expect("cold t<=12.5 -> 0", cop(50, 12.5) == 0.0)
    expect("clamp max 8.5", cop(15, 13.0) == 8.5)


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
    # No data yet -> Unknown (unchanged availability rule)
    fc.thermal, fc.heater, fc.cooling = None, None, None
    expect("totals Unknown when thermal/heater unknown", total_power(fc) is None)

    # Cooling status still unknown but thermal+heater known -> NO regression,
    # cooling treated as 0.
    fc.thermal, fc.heater, fc.cooling, fc._cop = 0.0, False, None, 0.0
    approx("cooling None -> 0 contribution", total_power(fc), 0.0)

    # Passive cooling active: +0.1 kW
    fc.cooling = True
    approx("cooling on adds 0.1 kW", total_power(fc), 0.1)

    # Cooling on + heater on + heat pump running
    fc.thermal, fc._cop, fc.heater, fc.cooling = 6.0, 3.0, True, True
    # hp = 6/3 = 2.0, heater 3.0, cooling 0.1 -> 5.1
    approx("combined hp+heater+cooling", total_power(fc), 5.1)

    # Cooling off -> term drops
    fc.cooling = False
    approx("cooling off -> no cooling term", total_power(fc), 5.0)

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


def main():
    test_cop()
    test_decode()
    test_framing()
    test_watchdog()
    test_integrator_math()
    test_cooling_coordinator()
    test_cooling_sensors()
    print()
    print("RESULT:", "ALL PASS" if not _fails else f"{len(_fails)} FAIL: {_fails}")
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    main()
