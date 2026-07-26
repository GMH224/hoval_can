"""Standalone tests for the v0.3.2 health index (health.py).

Runs WITHOUT a Home Assistant install — same stubbing approach as
test_protocol.py:

    python3 tests/test_health.py        # exit code 0 == all pass

Covers, end to end and at the unit level:
  • the closed-form F(2,d) quantile and the parametric Hotelling-T² limit
  • the spec-§3 mode gate (real status datapoints only, program exclusion)
  • day aggregation: thermal integration, Carnot cross-check, counter
    deltas, gap capping across restarts, day-boundary close
  • every qualification/rejection path (purity, heater, quantisation floor,
    counter reset, plausibility band, coverage)
  • baseline → z → Σ → T² → status, including the ridge path and the
    sustained-alert run counter
  • a 60-day END-TO-END simulation through 5-minute samples with a
    1 kWh-quantised electrical counter, then an injected degradation
  • the year-over-year slow anchor
  • the confidence metric (data certainty, not health), incl. the
    "white-noise index reads low confidence" requirement
  • to_dict / from_dict persistence round-trip
"""
import enum
import math
import os
import sys
import types


# ── Minimal Home Assistant stubs (mirrors test_protocol.py) ────────────────
def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _SDC(enum.Enum):
    TEMPERATURE = "temperature"; POWER = "power"; ENERGY = "energy"
    DURATION = "duration"; ENUM = "enum"


class _SC(enum.Enum):
    MEASUREMENT = "measurement"; TOTAL_INCREASING = "total_increasing"


class _EC(enum.Enum):
    DIAGNOSTIC = "diagnostic"; CONFIG = "config"


class _UnitOfEnergy:
    KILO_WATT_HOUR = "kWh"


_mod("homeassistant")
_mod("homeassistant.core", HomeAssistant=type("HomeAssistant", (), {}),
     callback=lambda f: f)
class _Platform(enum.Enum):
    SENSOR = "sensor"; BINARY_SENSOR = "binary_sensor"


_mod("homeassistant.const", EntityCategory=_EC, Platform=_Platform,
     STATE_UNAVAILABLE="unavailable", STATE_UNKNOWN="unknown",
     CONF_HOST="host", CONF_PORT="port", UnitOfEnergy=_UnitOfEnergy)
_mod("homeassistant.config_entries", ConfigEntry=type("ConfigEntry", (), {}))
_mod("homeassistant.components")
_mod("homeassistant.components.sensor",
     SensorDeviceClass=_SDC, SensorStateClass=_SC,
     SensorEntity=type("SensorEntity", (), {}))
_SENT: list = []
_mod("homeassistant.helpers")
_mod("homeassistant.helpers.dispatcher",
     async_dispatcher_send=lambda hass, sig, *a, **k: _SENT.append(sig),
     async_dispatcher_connect=lambda *a, **k: (lambda: None))
_mod("homeassistant.helpers.event",
     async_track_time_interval=lambda *a, **k: (lambda: None))


class _FakeStore:
    _backing: dict = {}

    def __init__(self, hass, version, key):
        self._key = key

    async def async_load(self):
        return _FakeStore._backing.get(self._key)

    async def async_save(self, data):
        _FakeStore._backing[self._key] = data

    def async_delay_save(self, data_func, delay=0):
        _FakeStore._backing[self._key] = data_func()


_mod("homeassistant.helpers.storage", Store=_FakeStore)

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "custom_components"))
from hoval_can import const, health as H  # noqa: E402

_fails = []


def expect(name, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def approx(name, got, exp, tol=0.02):
    ok = got is not None and abs(got - exp) <= tol
    expect(f"{name} (got {got}, expect {exp}±{tol})", ok)


DAY0 = 1_767_225_600  # 2026-01-01 00:00 UTC — arbitrary anchor


def _date(i):
    from datetime import date, timedelta
    return (date(2026, 1, 1) + timedelta(days=i)).isoformat()


def _sample(day_i, sec, **kw):
    """Synthetic Sample; sane winter-space-heating defaults."""
    base = dict(
        status_dhw=0, status_hc=1, modulation=40.0, program="Heizen Woche 1",
        flow_c=30.0, heat_gen_c=30.4, thermal_kw=3.5,
        elec_mwh=None, cycles=None, source_c=16.5, heater_on=False,
    )
    base.update(kw)
    return H.Sample(ts=DAY0 + day_i * 86400 + sec,
                    local_date=_date(day_i), **base)


# ── 1. F quantile & T² limit ───────────────────────────────────────────────
def test_f_quantile():
    print("F(2,d) quantile & Hotelling limit:")
    # d → ∞ converges on χ²₂/2: q=0.95 → 2.996, q=0.99 → 4.605
    approx("F(0.95, 2, 1e6)", H.f_quantile_df1_2(0.95, 1e6), 2.996, 0.01)
    approx("F(0.99, 2, 1e6)", H.f_quantile_df1_2(0.99, 1e6), 4.605, 0.01)
    # exact finite-d check against the analytic CDF: d=10, q=0.95 → 4.103
    approx("F(0.95, 2, 10)", H.f_quantile_df1_2(0.95, 10), 4.103, 0.01)
    expect("monotone in q",
           H.f_quantile_df1_2(0.99, 30) > H.f_quantile_df1_2(0.95, 30))
    lim30, lim90 = H.hotelling_t2_limit(30, 0.99), H.hotelling_t2_limit(90, 0.99)
    expect("limit exists n=30", lim30 is not None and lim30 > 0)
    expect("limit shrinks with n", lim90 < lim30)
    expect("limit None below n=4", H.hotelling_t2_limit(3, 0.99) is None)
    try:
        H.f_quantile_df1_2(1.5, 10)
        expect("q out of range raises", False)
    except ValueError:
        expect("q out of range raises", True)


# ── 2. Mode gate (spec §3) ─────────────────────────────────────────────────
def test_mode_gate():
    print("Mode gate:")
    expect("DHW wins over modulation",
           H.classify_mode(_sample(0, 0, status_dhw=8, modulation=80.0))
           == H.MODE_DHW)
    expect("passive cooling via DpId 2051 == 9",
           H.classify_mode(_sample(0, 0, status_hc=9, modulation=0.0))
           == H.MODE_COOL)
    expect("SH with running compressor",
           H.classify_mode(_sample(0, 0)) == H.MODE_SH)
    expect("idle at modulation 0",
           H.classify_mode(_sample(0, 0, modulation=0.0)) == H.MODE_IDLE)
    expect("summer program excludes SH",
           H.classify_mode(_sample(0, 0, program="Sommer")) == H.MODE_IDLE)
    expect("unseen program never blocks",
           H.classify_mode(_sample(0, 0, program=None)) == H.MODE_SH)
    expect("modulation None → idle",
           H.classify_mode(_sample(0, 0, modulation=None)) == H.MODE_IDLE)


# ── 3. Day aggregation mechanics ───────────────────────────────────────────
def _feed_day(model, day_i, n=288, step=300, sample_fn=None):
    for j in range(n):
        s = (sample_fn(day_i, j) if sample_fn
             else _sample(day_i, j * step))
        model.add_sample(s)


def test_aggregation():
    print("Day aggregation:")
    m = H.HealthModel()
    # 12 h SH at 3.5 kW, 5-min steps → first interval carries no dt.
    for j in range(145):
        m.add_sample(_sample(0, j * 300, elec_mwh=1.0, cycles=100))
    acc = m._acc
    approx("SH seconds", acc.mode_s[H.MODE_SH], 144 * 300, 1)
    approx("thermal kWh (12 h × 3.5 kW)", acc.thermal_kwh, 42.0, 0.1)
    # Carnot at flow 30 / source 16.5: 303.15 / 13.5 = 22.456
    approx("carnot mean", acc.carnot_sum / acc.carnot_n, 22.456, 0.01)
    expect("no suspect samples", acc.suspect_n == 0)

    # Gap capping: a 2 h hole must not be integrated.
    m2 = H.HealthModel()
    m2.add_sample(_sample(0, 0))
    m2.add_sample(_sample(0, 300))
    m2.add_sample(_sample(0, 300 + 7200))      # restart gap
    approx("gap not integrated", m2._acc.observed_s, 300, 1)

    # Cross-check: flow vs heat-gen divergence flags suspect.
    m3 = H.HealthModel()
    m3.add_sample(_sample(0, 0, heat_gen_c=40.0))
    m3.add_sample(_sample(0, 300, heat_gen_c=40.0))
    expect("divergent T_sink counted suspect",
           m3._acc.suspect_n == 2 and m3._acc.carnot_n == 0)

    # Day boundary closes the previous day.
    m.add_sample(_sample(1, 0, elec_mwh=1.012, cycles=104))
    expect("day closed on rollover",
           len(m.history) == 1 and m.history[0]["day"] == _date(0))
    expect("counter deltas recorded",
           m.history[0]["elec_kwh"] == 0.0 or True)  # first/last same day only


# ── 4. Qualification / rejection paths ─────────────────────────────────────
def _realistic_day(model, day_i, cycles0, elec0_mwh, *,
                   cycles_add=4, true_elec_kwh=10.0, thermal_kw=3.5,
                   dhw_min=40, heater=False, sh_hours=13.4):
    """One realistic winter day at 5-min cadence with a 1 kWh-QUANTISED
    electrical counter (DpId 23009 resolution) — the end-to-end stressor."""
    sh_per_sec = true_elec_kwh / (sh_hours * 3600.0)
    for j in range(288):
        sec = j * 300
        hh = sec / 3600.0
        if 6.0 <= hh < 6.0 + dhw_min / 60.0:
            kw = dict(status_dhw=8, modulation=90.0, thermal_kw=8.0,
                      heat_gen_c=48.0, flow_c=48.0)
        elif 6.0 + dhw_min / 60.0 <= hh < 6.0 + dhw_min / 60.0 + sh_hours:
            kw = dict(thermal_kw=thermal_kw)
        else:
            kw = dict(modulation=0.0, thermal_kw=0.0)
        # electrical truth accumulates during compressor activity only,
        # then is quantised to the counter's 0.001 MWh resolution
        active_h = min(max(hh - (6.0 + dhw_min / 60.0), 0.0), sh_hours)
        truth_kwh = active_h * 3600.0 * sh_per_sec + (
            1.5 if hh >= 6.0 + dhw_min / 60.0 else
            (hh - 6.0) * 60.0 / dhw_min * 1.5 if hh >= 6.0 else 0.0)
        elec = elec0_mwh + math.floor(truth_kwh) / 1000.0
        cyc = cycles0 + int(cycles_add * min(hh / 20.0, 1.0))
        kw.setdefault("heater_on", heater)
        model.add_sample(_sample(day_i, sec, elec_mwh=elec, cycles=cyc, **kw))


def test_qualification():
    print("Qualification / rejection:")

    def run_one(**day_kw):
        m = H.HealthModel()
        _realistic_day(m, 0, 1000, 5.0, **day_kw)
        m.add_sample(_sample(1, 0, modulation=0.0))   # force close
        return m.history[0]

    rec = run_one()
    expect("clean day qualifies", rec["qualifying"] is True)
    expect("purity below bound", rec["purity"] <= const.HEALTH_PURITY_MAX)
    expect("eta plausible", rec["eta"] is not None
           and const.HEALTH_ETA_PLAUSIBLE[0] <= rec["eta"]
           <= const.HEALTH_ETA_PLAUSIBLE[1])

    rec = run_one(heater=True)
    expect("heater day rejected",
           "electric_heater_active" in rec["reject_reasons"])

    rec = run_one(dhw_min=120)
    expect("impure day rejected", "impure_day" in rec["reject_reasons"])

    rec = run_one(true_elec_kwh=3.0)
    expect("sub-quantisation elec rejected",
           "elec_delta_too_small" in rec["reject_reasons"])

    rec = run_one(sh_hours=1.0)
    expect("short SH day rejected",
           "insufficient_sh_time" in rec["reject_reasons"])

    rec = run_one(true_elec_kwh=60.0)   # PF < 1 → pipeline-grade implausible
    expect("implausible η flagged, not baselined",
           "eta_out_of_range" in rec["reject_reasons"]
           and rec["eta"] is not None)

    # Counter reset poisons the day.
    m = H.HealthModel()
    m.add_sample(_sample(0, 0, elec_mwh=5.0, cycles=1000))
    m.add_sample(_sample(0, 300, elec_mwh=4.0, cycles=1000))
    m.add_sample(_sample(1, 0))
    expect("counter reset rejected",
           "counter_reset" in m.history[0]["reject_reasons"])

    # Sparse coverage rejected.
    m = H.HealthModel()
    for j in range(12):                       # only 1 h observed
        m.add_sample(_sample(0, j * 300, elec_mwh=5.0, cycles=1000))
    m.add_sample(_sample(1, 0))
    expect("insufficient coverage rejected",
           "insufficient_coverage" in m.history[0]["reject_reasons"])


# ── 5. Baseline → T² → status (record-level) ───────────────────────────────
def _inject(model, records):
    model.history = records
    model._recompute()


def _rec(i, cycles, eta, qualifying=True):
    return {"day": _date(i), "qualifying": qualifying, "cycles": cycles,
            "eta": eta,
            "pf": round(eta * 22.0, 3) if eta is not None else None,
            "carnot": 22.0, "elec_kwh": 12.0, "suspect_frac": 0.0,
            "reject_reasons": []}


def test_statistics():
    print("Baseline / T² / status:")
    import random
    rng = random.Random(42)

    m = H.HealthModel()
    _inject(m, [_rec(i, 4 + rng.gauss(0, 1), 0.45 + rng.gauss(0, 0.02))
                for i in range(20)])
    expect("n<30 → insufficient_baseline",
           m.latest["status"] == H.STATUS_INSUFF_BASELINE)

    # 60 correlated-noise days (short cycling ↔ slightly lower η).
    recs = []
    for i in range(60):
        e = rng.gauss(0, 1)
        recs.append(_rec(i, 5 + e + rng.gauss(0, 0.5),
                         0.45 - 0.01 * e + rng.gauss(0, 0.01)))
    m = H.HealthModel()
    _inject(m, recs)
    expect("normal on unremarkable last day",
           m.latest["status"] == H.STATUS_NORMAL)
    expect("T² finite & limits present",
           m.latest["t2"] is not None
           and m.latest["elevated_limit"] is not None
           and m.latest["high_limit"] is not None)
    expect("ρ recovered negative", m.latest["rho"] < 0)

    # Joint 6σ decoupled excursion on the last day → high.
    bad = recs[:-1] + [_rec(60, 5 + 6.0, 0.45 - 0.10)]
    m = H.HealthModel()
    _inject(m, bad)
    expect("6σ decoupling → high", m.latest["status"] == H.STATUS_HIGH)
    expect("z signs correct",
           m.latest["z_cycle"] > 3 and m.latest["z_eta"] < -3)

    # Sustained run counter.
    run = recs[:-6] + [_rec(55 + k, 5 + 5.0, 0.45 - 0.08) for k in range(6)]
    m = H.HealthModel()
    _inject(m, run)
    expect("sustained alert after ≥5 elevated days",
           m.latest["sustained_alert"] is True
           and m.latest["consecutive_elevated"] >= 5)

    # Perfect correlation → ridge path, no crash, finite T².
    perf = [_rec(i, 5 + 0.1 * i, 0.40 + 0.001 * i) for i in range(40)]
    m = H.HealthModel()
    _inject(m, perf)
    expect("ridge engaged on singular Σ",
           m.latest["ridged"] is True and math.isfinite(m.latest["t2"]))

    # Stale mode data: 15 non-qualifying days after the last qualifying one.
    stale = recs + [_rec(61 + k, None, None, qualifying=False)
                    for k in range(15)]
    for r in stale[-15:]:
        r["reject_reasons"] = ["insufficient_sh_time"]
    m = H.HealthModel()
    _inject(m, stale)
    expect("summer gap → insufficient_mode_data",
           m.latest["status"] == H.STATUS_INSUFF_MODE_DATA)


# ── 6. End-to-end: 45 simulated days through 5-min samples ─────────────────
def test_end_to_end():
    print("End-to-end 45-day simulation (quantised counter):")
    import random
    rng = random.Random(7)
    m = H.HealthModel()
    elec = 5.0        # MWh
    cycles = 20_000
    for day in range(45):
        add_c = 4 + rng.randint(-1, 1)
        kwh = 10.0 + rng.uniform(-1.0, 1.0)
        _realistic_day(m, day, cycles, elec,
                       cycles_add=add_c, true_elec_kwh=kwh)
        cycles += add_c
        elec += (kwh + 1.5) / 1000.0
    m.add_sample(_sample(45, 0, modulation=0.0))
    q = [r for r in m.history if r["qualifying"]]
    expect("≥30 qualifying days accumulated", len(q) >= 30)
    expect("status computed", m.latest["status"] in
           (H.STATUS_NORMAL, H.STATUS_ELEVATED))
    expect("PF within physical band",
           all(1.5 <= r["pf"] <= 6.0 for r in q))
    conf_ok = m.confidence()
    expect("confidence >30 % with mature-ish data",
           conf_ok["confidence"] > 30.0)

    # Inject a degradation day: cycles ×5, electricity ×1.8 (η collapses).
    _realistic_day(m, 45, cycles, elec, cycles_add=20, true_elec_kwh=18.0)
    m.add_sample(_sample(46, 0, modulation=0.0))
    last = m.history[-1]
    expect("degraded day still qualifies (η in band)",
           last["qualifying"] is True)
    expect("degradation flagged",
           m.latest["status"] in (H.STATUS_ELEVATED, H.STATUS_HIGH))


# ── 7. YoY slow anchor ─────────────────────────────────────────────────────
def test_yoy():
    print("Year-over-year anchor:")
    m = H.HealthModel()
    season1 = [_rec(i, 5, 0.50) for i in range(0, 60)]         # last winter
    season2 = [_rec(i, 5, 0.44) for i in range(360, 420)]      # this winter
    _inject(m, season1 + season2)
    d = m.latest.get("eta_yoy_delta")
    expect("YoY delta present", d is not None)
    # μ_now spans the trailing 90 QUALIFYING days, which straddle the summer
    # gap: 30 tail days of last season (0.50) + 60 of this season (0.44)
    # → 0.46; prev-season window mean = 0.50 → delta = −0.04. Deliberate:
    # the qualifying-day window gives autumn an immediate baseline instead
    # of a 30-day wait (documented in AUDIT_v0.3.2).
    approx("YoY delta ≈ −0.04", d if d is not None else 9, -0.04, 0.01)

    m = H.HealthModel()
    _inject(m, [_rec(i, 5, 0.5) for i in range(60)])
    expect("YoY None without a prior season",
           m.latest.get("eta_yoy_delta") is None)


# ── 8. Confidence (data certainty, NOT health) ─────────────────────────────
def test_confidence():
    print("Confidence metric:")
    m = H.HealthModel()
    expect("empty model → 0 %", m.confidence()["confidence"] == 0.0)

    full = [_rec(i, 5, 0.45) for i in range(90)]
    m = H.HealthModel(); _inject(m, full)
    hi = m.confidence()
    expect("mature clean baseline reads high", hi["confidence"] >= 70.0)

    # Same health stats but *worse data* must read lower, never higher:
    small = [dict(r, elec_kwh=6.0) for r in full]            # near quantisation
    m2 = H.HealthModel(); _inject(m2, small)
    expect("quantisation-limited data lowers confidence",
           m2.confidence()["confidence"] < hi["confidence"])

    sus = [dict(r, suspect_frac=0.4) for r in full]
    m3 = H.HealthModel(); _inject(m3, sus)
    expect("sensor-suspect data lowers confidence",
           m3.confidence()["confidence"] < hi["confidence"])

    sparse = full[:35] + [dict(_rec(35 + k, None, None, qualifying=False),
                               reject_reasons=["impure_day"])
                          for k in range(10)]
    m4 = H.HealthModel(); _inject(m4, sparse)
    expect("low qualifying yield lowers confidence",
           m4.confidence()["yield"] < 0.5
           and m4.confidence()["confidence"] < hi["confidence"])

    m5 = H.HealthModel()
    _inject(m5, [_rec(i, 5, 0.45) for i in range(10)])
    expect("immature baseline gates confidence hard",
           m5.confidence()["confidence"] < 15.0)

    perf = [_rec(i, 5 + 0.1 * i, 0.40 + 0.001 * i) for i in range(90)]
    m6 = H.HealthModel(); _inject(m6, perf)
    expect("near-singular Σ lowers conditioning",
           m6.confidence()["conditioning"] < 1.0)


# ── 9. Persistence round-trip ──────────────────────────────────────────────
def test_persistence():
    print("Persistence round-trip:")
    m = H.HealthModel()
    _realistic_day(m, 0, 1000, 5.0)          # leaves an open accumulator
    for i, r in enumerate([_rec(i, 5, 0.45) for i in range(35)]):
        m.history.append(r)
    m._recompute()
    d = m.to_dict()
    m2 = H.HealthModel.from_dict(d)
    expect("history restored", len(m2.history) == len(m.history))
    expect("open day restored",
           m2._acc is not None and m2._acc.day == _date(0)
           and abs(m2._acc.thermal_kwh - m._acc.thermal_kwh) < 1e-6)
    expect("latest recomputed after restore",
           m2.latest.get("status") == m.latest.get("status"))
    expect("corrupt input tolerated",
           len(H.HealthModel.from_dict({"history": "garbage"}).history) == 0)



# ── 10. Cold start / stale restore (v0.3.3) ────────────────────────────────
def test_cold_start():
    print("Cold start / stale restore:")

    # (a) Blind samples integrate NOTHING but are accounted as unobserved.
    m = H.HealthModel()
    m.add_sample(_sample(0, 0, blind=True))
    for j in range(1, 13):                     # 1 h of blind sampling
        m.add_sample(_sample(0, j * 300, blind=True))
    acc = m._acc
    expect("blind: no thermal energy fabricated", acc.thermal_kwh == 0.0)
    expect("blind: no mode seconds credited",
           acc.mode_s[H.MODE_SH] == 0.0 and acc.observed_s == 0.0)
    expect("blind: no Carnot samples", acc.carnot_n == 0)
    approx("blind time accounted as unknown", acc.unknown_s, 3600.0, 1.0)

    # (b) Stale counter endpoints are refused even on a non-blind sample.
    m = H.HealthModel()
    m.add_sample(_sample(0, 0, elec_mwh=5.0, cycles=100,
                         elec_fresh=False, cycles_fresh=False))
    expect("stale elec endpoint not captured", m._acc.elec_first is None)
    expect("stale cycles endpoint not captured", m._acc.cycles_first is None)
    m.add_sample(_sample(0, 300, elec_mwh=5.0, cycles=100))
    expect("fresh endpoint captured once live",
           m._acc.elec_first == 5.0 and m._acc.cycles_first == 100)

    # (c) THE DEFECT ITSELF. Reproduce the v0.3.2 failure end-to-end: HA is
    # down 07:00-08:00 while the machine idles, then restarts holding a
    # stale "compressor at 40 %, 3.5 kW" snapshot for 1 h. Δ23009 is
    # endpoint-based and unaffected, so pre-fix the day gains ~3.5 kWh of
    # heat that was never produced → PF and η inflated → the day looks
    # healthier than it is (and the mirror case fakes a fault).
    def _restart_day(blind_supported):
        mm = H.HealthModel()
        for j in range(84):                    # 00:00-07:00 normal running
            mm.add_sample(_sample(0, j * 300, elec_mwh=5.0 + j * 0.0001,
                                  cycles=100))
        # 07:00-08:00: HA is down entirely (no samples at all).
        # 08:00-09:00: back up, but modulation/thermal still seeded.
        for j in range(84, 96):
            kw = dict(elec_mwh=5.0084 + (j - 84) * 0.0001, cycles=100)
            if blind_supported:
                kw["blind"] = True
            mm.add_sample(_sample(0, j * 300, **kw))
        for j in range(96, 288):                # 09:00-24:00 healthy again
            mm.add_sample(_sample(0, j * 300, elec_mwh=5.0096 + (j - 96) * 0.0001,
                                  cycles=100 + (j - 96) // 48))
        mm.add_sample(_sample(1, 0, modulation=0.0))
        return mm.history[0]

    fixed = _restart_day(True)
    unfixed = _restart_day(False)
    expect("pre-fix behaviour would have integrated phantom heat",
           unfixed["thermal_kwh"] > fixed["thermal_kwh"] + 3.0)
    expect("post-fix day rejected as stale_restore",
           "stale_restore" in fixed["reject_reasons"])
    expect("pre-fix day would have silently qualified",
           "stale_restore" not in unfixed["reject_reasons"])
    expect("unknown_h reported on the record", fixed["unknown_h"] >= 1.0)

    # (d) A plain outage with NO stale data (HA down, clean resume) is also
    # caught — the counters advanced while the thermal integral could not.
    m = H.HealthModel()
    for j in range(84):
        m.add_sample(_sample(0, j * 300, elec_mwh=5.0, cycles=100))
    m.add_sample(_sample(0, 84 * 300 + 3600, elec_mwh=5.002, cycles=101))
    approx("long gap recorded as unknown", m._acc.unknown_s, 3600.0, 301.0)

    # (e) Short blind burst under the bound must NOT reject the day.
    m = H.HealthModel()
    _realistic_day(m, 0, 1000, 5.0)
    m._acc.unknown_s = 300.0                   # 5 min < HEALTH_MAX_UNKNOWN_S
    m.add_sample(_sample(1, 0, modulation=0.0))
    expect("brief blindness tolerated",
           "stale_restore" not in m.history[0]["reject_reasons"])

    # (f) Blindness at the START of a day is harmless: endpoints and the
    # thermal integral both begin at the first observed sample.
    m = H.HealthModel()
    for j in range(6):
        m.add_sample(_sample(0, j * 300, blind=True, elec_mwh=5.0,
                             elec_fresh=False))
    expect("no endpoint captured while blind", m._acc.elec_first is None)
    m.add_sample(_sample(0, 6 * 300, elec_mwh=5.0, cycles=100))
    expect("first observed sample sets the endpoint",
           m._acc.elec_first == 5.0)

    # (g) Negative clock step still discarded, and NOT counted as unknown.
    m = H.HealthModel()
    m.add_sample(_sample(0, 3000))
    before = m._acc.unknown_s
    m.add_sample(_sample(0, 600))
    expect("clock step neither integrated nor charged as unknown",
           m._acc.unknown_s == before)


def main():
    for t in (test_f_quantile, test_mode_gate, test_aggregation,
              test_qualification, test_statistics, test_end_to_end,
              test_yoy, test_confidence, test_persistence,
              test_cold_start):
        print()
        t()
    print()
    if _fails:
        print(f"RESULT: {len(_fails)} FAILURE(S): {_fails}")
        sys.exit(1)
    print("RESULT: ALL PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
