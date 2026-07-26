"""Heat-pump health index for the Hoval CAN integration (v0.3.2).

Implements the corrected "Hoval Heat Pump Health Index" specification:
two MEASURED daily features fused with Hotelling's T² against the unit's
own rolling baseline. See const.py ("Health index") for the invariants and
AUDIT_v0.3.2.md for the derivation and validation.

Layout — two layers, deliberately separated:

  HealthModel    Pure Python. No Home Assistant imports, no clocks, no I/O.
                 Fed timestamped samples; owns day aggregation, baselines,
                 z-scores, Σ, T², status, and the confidence metric.
                 Fully serialisable (to_dict/from_dict) and unit-testable
                 standalone (tests/test_health.py).

  HealthTracker  HA glue. A 5-minute timer reads the coordinator, builds a
                 Sample, feeds the model, persists it via the Store helper
                 (debounced), and dispatches health_signal() so the three
                 health entities update.

Why the synthetic COP is NOT an input (the v0.3.1-era design error this
module corrects): calculate_cop() is a piecewise model of modulation and
temperatures — it contains no measured electrical quantity. A Gütegrad
built on it divides one temperature model by another and cannot move when
the machine actually degrades. Here PF(day) = measured thermal energy
(DpId 29051) / measured electrical energy (hardware counter DpId 23009),
so η(day) = PF / mean Carnot COP is a genuine second-law efficiency.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    DHW_STATUS_CHARGING, DOMAIN,
    HEALTH_FRESH_REQUIRED, HEALTH_SETTLE_REQUIRED, HEALTH_SETTLE_S,
    DP_FLOW_TEMP, DP_HEAT_GEN, DP_HEATING_PROGRAM, DP_MODULATION,
    DP_STATUS_HC, DP_STATUS_WW, DP_THERMAL_POWER, DP_WEZ_CYCLES,
    DP_WEZ_ELEC_TOTAL, HC_STATUS_PASSIVE_COOLING,
    COMPRESSOR_RUNNING_MODULATION,
    HEALTH_ALERT_RUN_DAYS, HEALTH_BASELINE_MIN, HEALTH_BASELINE_WINDOW,
    HEALTH_CONF_ELEC_FULL_KWH, HEALTH_CONF_W_CONDITION,
    HEALTH_CONF_W_RESOLUTION, HEALTH_CONF_W_SENSOR, HEALTH_CONF_W_YIELD,
    HEALTH_CONF_YIELD_WINDOW, HEALTH_ELEVATED_PCTL, HEALTH_ETA_PLAUSIBLE,
    HEALTH_EXCLUDED_PROGRAMS, HEALTH_HIGH_F_Q, HEALTH_HISTORY_MAX_DAYS,
    HEALTH_MAX_GAP_S, HEALTH_MAX_SUSPECT_FRAC, HEALTH_MAX_UNKNOWN_S,
    HEALTH_MIN_CARNOT_SAMPLES,
    HEALTH_MIN_COVERAGE_S, HEALTH_MIN_ELEC_KWH, HEALTH_MIN_SH_S,
    HEALTH_PURITY_MAX, HEALTH_RIDGE_EPS, HEALTH_SAMPLE_INTERVAL_S,
    HEALTH_SIGMA_FLOOR, HEALTH_STALE_MODE_DAYS, HEALTH_STORE_SUFFIX,
    HEALTH_TSINK_XCHECK_MAX_C, HEALTH_YOY_TOLERANCE_DAYS,
    PERSIST_SAVE_DELAY_S, STORAGE_VERSION,
    health_signal,
)

_LOGGER = logging.getLogger(__name__)

# Operating modes (spec §3). Only MODE_SH feeds the fused index.
MODE_SH   = "space_heating_active"
MODE_DHW  = "dhw"
MODE_COOL = "passive_cooling"
MODE_IDLE = "idle"

# Status flags (spec §11)
STATUS_NORMAL            = "normal"
STATUS_ELEVATED          = "elevated"
STATUS_HIGH              = "high"
STATUS_INSUFF_BASELINE   = "insufficient_baseline"
STATUS_INSUFF_MODE_DATA  = "insufficient_mode_data"
HEALTH_STATUS_OPTIONS = (
    STATUS_NORMAL, STATUS_ELEVATED, STATUS_HIGH,
    STATUS_INSUFF_BASELINE, STATUS_INSUFF_MODE_DATA,
)


def f_quantile_df1_2(q: float, d: float) -> float:
    """Inverse CDF of the F(2, d) distribution — closed form.

    For numerator df = 2 the F CDF is 1 − (1 + 2x/d)^(−d/2), which inverts
    exactly:  x = (d/2)·((1 − q)^(−2/d) − 1).  As d → ∞ this converges to
    the χ²₂/2 quantile (e.g. q=0.95 → 2.996), which anchors the sanity test.
    Used for the parametric Hotelling-T² "high" control limit with p = 2 —
    no SciPy dependency required.
    """
    if not 0.0 < q < 1.0 or d <= 0.0:
        raise ValueError("q must be in (0,1) and d > 0")
    return (d / 2.0) * ((1.0 - q) ** (-2.0 / d) - 1.0)


def hotelling_t2_limit(n: int, q: float) -> float | None:
    """Parametric T² control limit for p = 2 and a FUTURE observation.

    T² ~ [p(n+1)(n−1) / (n(n−p))] · F(p, n−p);  with p = 2:
        limit = [2(n+1)(n−1) / (n(n−2))] · F_q(2, n−2)
    Approximate for this pipeline (the z's share the estimation window with
    the observation being scored) — treated as an upper "high" bound, with
    the empirical 95th percentile as the primary "elevated" trigger.
    """
    if n < 4:
        return None
    coef = (2.0 * (n + 1) * (n - 1)) / (n * (n - 2))
    return coef * f_quantile_df1_2(q, float(n - 2))


def _percentile(sorted_vals: list[float], q: float) -> float | None:
    """Linear-interpolated empirical percentile of a pre-sorted list."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return sorted_vals[0]
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def _mean_std(vals: list[float]) -> tuple[float, float]:
    n = len(vals)
    mu = sum(vals) / n
    var = sum((v - mu) ** 2 for v in vals) / (n - 1) if n > 1 else 0.0
    return mu, max(math.sqrt(var), HEALTH_SIGMA_FLOOR)


@dataclass
class Sample:
    """One telemetry snapshot handed to the model by the tracker."""
    ts: float                       # unix seconds (monotonic ordering assumed)
    local_date: str                 # YYYY-MM-DD in the *local* timezone
    status_dhw: int | None
    status_hc: int | None
    modulation: float | None
    program: str | None
    flow_c: float | None
    heat_gen_c: float | None
    thermal_kw: float | None
    elec_mwh: float | None          # DpId 23009 (hardware counter)
    cycles: int | None              # DpId 2080 (hardware counter)
    source_c: float                 # configured ground-loop temperature
    heater_on: bool | None          # coordinator.electric_heater_on
    # v0.3.3 cold-start guards. `blind` means one or more model-critical
    # datapoints are still seeded from the Store, so this interval must be
    # accounted as unobserved rather than integrated. The two *_fresh flags
    # gate the hardware-counter endpoints independently, because a stale
    # endpoint corrupts Δ directly (see const.py "Cold-start readiness").
    blind: bool = False
    elec_fresh: bool = True
    cycles_fresh: bool = True


def classify_mode(s: Sample) -> str:
    """Spec §3 mode gate — real status datapoints only.

    DHW wins (compressor may be modulating for the tank, not the house);
    passive cooling is DpId 2051 == 9 (NOT the derived cooling-energy
    estimate — that entity is a configured plug value, excluded by design
    principle 2); SPACE_HEATING_ACTIVE requires a running compressor and a
    heating program that is not a summer/idle program. An unseen program
    datapoint never blocks classification.
    """
    if s.status_dhw == DHW_STATUS_CHARGING:
        return MODE_DHW
    if s.status_hc == HC_STATUS_PASSIVE_COOLING:
        return MODE_COOL
    running = (s.modulation or 0.0) > COMPRESSOR_RUNNING_MODULATION
    if running:
        prog = (s.program or "").strip().lower()
        if prog and any(x in prog for x in HEALTH_EXCLUDED_PROGRAMS):
            return MODE_IDLE
        return MODE_SH
    return MODE_IDLE


@dataclass
class _DayAccumulator:
    """In-progress aggregation of one local calendar day."""
    day: str
    observed_s: float = 0.0
    unknown_s: float = 0.0          # v0.3.3 — mid-day time we could not observe
    mode_s: dict[str, float] = field(
        default_factory=lambda: {m: 0.0 for m in
                                 (MODE_SH, MODE_DHW, MODE_COOL, MODE_IDLE)})
    heater_s: float = 0.0
    thermal_kwh: float = 0.0        # ∫ 29051 over compressor-running samples
    carnot_sum: float = 0.0
    carnot_n: int = 0
    suspect_n: int = 0
    sh_sample_n: int = 0
    elec_first: float | None = None  # MWh (23009)
    elec_last: float | None = None
    cycles_first: int | None = None
    cycles_last: int | None = None
    counter_reset: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day, "observed_s": self.observed_s,
            "unknown_s": self.unknown_s,
            "mode_s": dict(self.mode_s), "heater_s": self.heater_s,
            "thermal_kwh": self.thermal_kwh, "carnot_sum": self.carnot_sum,
            "carnot_n": self.carnot_n, "suspect_n": self.suspect_n,
            "sh_sample_n": self.sh_sample_n,
            "elec_first": self.elec_first, "elec_last": self.elec_last,
            "cycles_first": self.cycles_first, "cycles_last": self.cycles_last,
            "counter_reset": self.counter_reset,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> _DayAccumulator:
        acc = cls(day=str(d.get("day", "")))
        acc.observed_s   = float(d.get("observed_s", 0.0))
        acc.unknown_s    = float(d.get("unknown_s", 0.0))
        stored_modes     = d.get("mode_s") or {}
        for m in acc.mode_s:
            acc.mode_s[m] = float(stored_modes.get(m, 0.0))
        acc.heater_s     = float(d.get("heater_s", 0.0))
        acc.thermal_kwh  = float(d.get("thermal_kwh", 0.0))
        acc.carnot_sum   = float(d.get("carnot_sum", 0.0))
        acc.carnot_n     = int(d.get("carnot_n", 0))
        acc.suspect_n    = int(d.get("suspect_n", 0))
        acc.sh_sample_n  = int(d.get("sh_sample_n", 0))
        acc.elec_first   = d.get("elec_first")
        acc.elec_last    = d.get("elec_last")
        acc.cycles_first = d.get("cycles_first")
        acc.cycles_last  = d.get("cycles_last")
        acc.counter_reset = bool(d.get("counter_reset", False))
        return acc


class HealthModel:
    """Pure statistical core — no HA, no clocks, no I/O."""

    def __init__(self) -> None:
        self._acc: _DayAccumulator | None = None
        self._last_ts: float | None = None
        # Closed day records, oldest → newest, capped at HEALTH_HISTORY_MAX_DAYS.
        self.history: list[dict[str, Any]] = []
        # Derived state, refreshed by _recompute() after each day close.
        self.latest: dict[str, Any] = {}

    # ── Sample ingestion ──────────────────────────────────────────────────

    def add_sample(self, s: Sample) -> bool:
        """Feed one sample. Returns True if a day was closed (state changed)."""
        closed = False
        if self._acc is not None and s.local_date != self._acc.day:
            self._close_day()
            closed = True
        if self._acc is None:
            self._acc = _DayAccumulator(day=s.local_date)
            self._last_ts = None    # never integrate across a day boundary

        acc = self._acc
        # Interval since the previous sample of the SAME day. An over-long
        # interval is never integrated — but it is no longer silently
        # discarded either (v0.3.3): the hardware counters kept advancing
        # while we were not looking, so that time is recorded as UNOBSERVED
        # and weighed at day close. A negative step (clock adjustment) is
        # dropped outright; it carries no reliable duration.
        dt_s = 0.0
        if self._last_ts is not None:
            raw = s.ts - self._last_ts
            if raw < 0.0:
                dt_s = 0.0
            elif raw > HEALTH_MAX_GAP_S:
                acc.unknown_s += raw
                dt_s = 0.0
            else:
                dt_s = raw
        self._last_ts = s.ts

        # Stale-restore blindness (v0.3.3): the tracker could not obtain a
        # trustworthy view of the machine for this interval. Record the time
        # as unobserved and integrate NOTHING — not thermal energy, not mode
        # seconds, not counter endpoints. Half-trusting a seeded value is how
        # a restart gets mistaken for a fault.
        if s.blind:
            acc.unknown_s += dt_s
            return closed

        mode = classify_mode(s)
        acc.observed_s += dt_s
        acc.mode_s[mode] += dt_s
        if s.heater_on:
            acc.heater_s += dt_s

        # Thermal-energy integration (left Riemann over the closing interval)
        # whenever the compressor is running — on purity days this is, by
        # construction, space-heating energy to within the purity bound.
        running = (s.modulation or 0.0) > COMPRESSOR_RUNNING_MODULATION
        if running and s.thermal_kw is not None and dt_s > 0.0:
            acc.thermal_kwh += max(0.0, float(s.thermal_kw)) * (dt_s / 3600.0)

        # Daily Carnot mean over SH samples with the flow/heat-gen cross-check
        # (spec §5): T_sink = flow temperature; heat-generator temperature is
        # the plausibility witness, |Δ| > 3 °C → sensor-suspect, excluded.
        if mode == MODE_SH:
            acc.sh_sample_n += 1
            if s.flow_c is not None and s.heat_gen_c is not None:
                if abs(s.flow_c - s.heat_gen_c) > HEALTH_TSINK_XCHECK_MAX_C:
                    acc.suspect_n += 1
                elif s.flow_c > s.source_c:      # lift must be positive
                    t_sink_k = s.flow_c + 273.15
                    acc.carnot_sum += t_sink_k / (s.flow_c - s.source_c)
                    acc.carnot_n += 1

        # Hardware-counter endpoints. A decreasing counter (device swap /
        # rollover) poisons the whole day — flag it, never clamp it.
        if s.elec_mwh is not None and s.elec_fresh:
            if acc.elec_first is None:
                acc.elec_first = float(s.elec_mwh)
            elif float(s.elec_mwh) < acc.elec_first - 1e-9:
                acc.counter_reset = True
            acc.elec_last = float(s.elec_mwh)
        if s.cycles is not None and s.cycles_fresh:
            c = int(s.cycles)
            if acc.cycles_first is None:
                acc.cycles_first = c
            elif c < acc.cycles_first:
                acc.counter_reset = True
            acc.cycles_last = c

        return closed

    # ── Day close & qualification (spec §4-§6) ────────────────────────────

    def _close_day(self) -> None:
        acc = self._acc
        self._acc = None
        if acc is None:
            return

        reject: list[str] = []
        sh_s = acc.mode_s[MODE_SH]
        impure_s = acc.mode_s[MODE_DHW] + acc.mode_s[MODE_COOL]
        purity = (impure_s / acc.observed_s) if acc.observed_s > 0 else 1.0

        elec_kwh = None
        if acc.elec_first is not None and acc.elec_last is not None:
            elec_kwh = (acc.elec_last - acc.elec_first) * 1000.0
        cycles = None
        if acc.cycles_first is not None and acc.cycles_last is not None:
            cycles = acc.cycles_last - acc.cycles_first

        if acc.counter_reset:
            reject.append("counter_reset")
        if acc.unknown_s > HEALTH_MAX_UNKNOWN_S:
            # Structural mismatch, not a data-quantity complaint: Δ23009
            # includes every kWh drawn during the unobserved window while
            # thermal_kwh cannot, so PF is understated by construction.
            reject.append("stale_restore")
        if acc.observed_s < HEALTH_MIN_COVERAGE_S:
            reject.append("insufficient_coverage")
        if sh_s < HEALTH_MIN_SH_S:
            reject.append("insufficient_sh_time")
        if purity > HEALTH_PURITY_MAX:
            reject.append("impure_day")             # spec §4: flag, don't fudge
        if acc.heater_s > 0.0:
            reject.append("electric_heater_active")  # kWh in Δ23009 ≠ compressor
        if elec_kwh is None or elec_kwh < HEALTH_MIN_ELEC_KWH:
            reject.append("elec_delta_too_small")    # 1 kWh quantisation guard
        if cycles is None:
            reject.append("no_cycle_data")
        if acc.carnot_n < HEALTH_MIN_CARNOT_SAMPLES:
            reject.append("insufficient_carnot_samples")
        if acc.sh_sample_n > 0 and (
                acc.suspect_n / acc.sh_sample_n) > HEALTH_MAX_SUSPECT_FRAC:
            reject.append("sensor_suspect")

        pf = eta = carnot = None
        if not reject:
            carnot = acc.carnot_sum / acc.carnot_n
            pf = acc.thermal_kwh / elec_kwh if elec_kwh else None
            if pf is not None and carnot > 0.0:
                eta = pf / carnot
                lo, hi = HEALTH_ETA_PLAUSIBLE
                if not lo <= eta <= hi:
                    # Audit rule: implausible Gütegrad ⇒ suspect the data
                    # pipeline before the machine — record, don't baseline.
                    reject.append("eta_out_of_range")

        record = {
            "day": acc.day,
            "observed_h": round(acc.observed_s / 3600.0, 2),
            "unknown_h": round(acc.unknown_s / 3600.0, 3),
            "sh_h": round(sh_s / 3600.0, 2),
            "dhw_h": round(acc.mode_s[MODE_DHW] / 3600.0, 2),
            "cool_h": round(acc.mode_s[MODE_COOL] / 3600.0, 2),
            "heater_h": round(acc.heater_s / 3600.0, 2),
            "purity": round(purity, 4),
            "suspect_frac": round(
                acc.suspect_n / acc.sh_sample_n, 3) if acc.sh_sample_n else 0.0,
            "thermal_kwh": round(acc.thermal_kwh, 2),
            "elec_kwh": round(elec_kwh, 1) if elec_kwh is not None else None,
            "cycles": cycles,
            "carnot": round(carnot, 3) if carnot is not None else None,
            "pf": round(pf, 3) if pf is not None else None,
            "eta": round(eta, 4) if eta is not None else None,
            "qualifying": not reject,
            "reject_reasons": reject,
        }
        self.history.append(record)
        if len(self.history) > HEALTH_HISTORY_MAX_DAYS:
            del self.history[: len(self.history) - HEALTH_HISTORY_MAX_DAYS]
        self._recompute()

    # ── Baseline, standardisation, fusion (spec §6-§9) ────────────────────

    def _qualifying_window(self) -> list[dict[str, Any]]:
        q = [r for r in self.history if r.get("qualifying")]
        return q[-HEALTH_BASELINE_WINDOW:]

    def _recompute(self) -> None:
        win = self._qualifying_window()
        n = len(win)
        out: dict[str, Any] = {
            "baseline_n": n,
            "last_day": self.history[-1]["day"] if self.history else None,
            "last_qualifying_day": win[-1]["day"] if win else None,
        }

        # insufficient_mode_data: the newest closed day is far past the last
        # qualifying one (e.g. summer — spec §11). Checked before baseline
        # size so a long idle season reads as "no mode data", not "immature".
        stale = False
        if self.history and win:
            try:
                gap = (date.fromisoformat(self.history[-1]["day"])
                       - date.fromisoformat(win[-1]["day"])).days
                stale = gap >= HEALTH_STALE_MODE_DAYS
            except ValueError:
                pass
        elif self.history and not win:
            stale = len(self.history) >= HEALTH_STALE_MODE_DAYS

        if n < HEALTH_BASELINE_MIN:
            out["status"] = (STATUS_INSUFF_MODE_DATA if stale
                             else STATUS_INSUFF_BASELINE)
            self.latest = out
            return

        cyc = [float(r["cycles"]) for r in win]
        eta = [float(r["eta"]) for r in win]
        mu_c, sd_c = _mean_std(cyc)
        mu_e, sd_e = _mean_std(eta)
        z_c = [(v - mu_c) / sd_c for v in cyc]
        z_e = [(v - mu_e) / sd_e for v in eta]

        # Σ of the standardised pair over the window (≈ correlation matrix;
        # diagonal ≈ 1 by construction). Ridge-regularise when near-singular
        # (spec §8) — near-perfect |ρ| makes Σ⁻¹ explode otherwise.
        m = len(z_c)
        s11 = sum(v * v for v in z_c) / (m - 1)
        s22 = sum(v * v for v in z_e) / (m - 1)
        s12 = sum(a * b for a, b in zip(z_c, z_e)) / (m - 1)
        rho = s12 / math.sqrt(s11 * s22) if s11 > 0 and s22 > 0 else 0.0
        det = s11 * s22 - s12 * s12
        ridged = det < HEALTH_RIDGE_EPS
        if ridged:
            s11 += HEALTH_RIDGE_EPS
            s22 += HEALTH_RIDGE_EPS
            det = s11 * s22 - s12 * s12

        def t2(zc: float, ze: float) -> float:
            # Z' Σ⁻¹ Z with the analytic 2×2 inverse.
            return max(0.0, (s22 * zc * zc - 2.0 * s12 * zc * ze
                             + s11 * ze * ze) / det)

        t2_all = [t2(a, b) for a, b in zip(z_c, z_e)]
        # Self-masking guard (found by test, documented in AUDIT_v0.3.2): a
        # genuine sustained fault sits INSIDE its own trailing window and
        # would lift the empirical 95th percentile above itself, silencing
        # exactly the alert the persistence rule exists for. The percentile
        # reference pool therefore excludes the trailing HEALTH_ALERT_RUN_DAYS
        # days being judged, whenever enough history remains.
        pool = (t2_all[:-HEALTH_ALERT_RUN_DAYS]
                if len(t2_all) - HEALTH_ALERT_RUN_DAYS >= HEALTH_BASELINE_MIN
                else t2_all)
        elevated_lim = _percentile(sorted(pool), HEALTH_ELEVATED_PCTL)
        high_lim = hotelling_t2_limit(n, HEALTH_HIGH_F_Q)

        latest = win[-1]
        z_c_last, z_e_last = z_c[-1], z_e[-1]
        t2_last = t2_all[-1]

        if stale:
            status = STATUS_INSUFF_MODE_DATA
        elif high_lim is not None and t2_last > high_lim:
            status = STATUS_HIGH
        elif elevated_lim is not None and t2_last > elevated_lim:
            status = STATUS_ELEVATED
        else:
            status = STATUS_NORMAL

        # Sustained-alert run: consecutive qualifying days above "elevated"
        # counted over the window's own T² sequence (spec §9: ≥5).
        run = 0
        for v in reversed(t2_all):
            if elevated_lim is not None and v > elevated_lim:
                run += 1
            else:
                break

        out.update({
            "status": status,
            "t2": round(t2_last, 3),
            "z_cycle": round(z_c_last, 3),
            "z_eta": round(z_e_last, 3),
            "cycle_rate": latest["cycles"],
            "eta": latest["eta"],
            "pf": latest["pf"],
            "carnot": latest["carnot"],
            "mu_cycle": round(mu_c, 2), "sigma_cycle": round(sd_c, 3),
            "mu_eta": round(mu_e, 4), "sigma_eta": round(sd_e, 5),
            "rho": round(rho, 3),
            "ridged": ridged,
            "elevated_limit": round(elevated_lim, 3)
                              if elevated_lim is not None else None,
            "high_limit": round(high_lim, 3) if high_lim is not None else None,
            "consecutive_elevated": run,
            "sustained_alert": run >= HEALTH_ALERT_RUN_DAYS,
            "eta_yoy_delta": self._eta_yoy_delta(win, mu_e),
        })
        self.latest = out

    def _eta_yoy_delta(self, win: list[dict[str, Any]],
                       mu_eta_now: float) -> float | None:
        """Season-matched slow anchor: current μ_eta minus μ_eta of the same
        calendar window one year earlier.

        The rolling baseline adapts weekly and therefore tracks — and hides —
        gradual multi-month degradation. This year-over-year delta is the
        deliberately NON-adaptive reference that makes slow drift visible
        (audit: "adaptive baseline blindness"). None until a second heating
        season exists.
        """
        if not win:
            return None
        try:
            centre = date.fromisoformat(win[-1]["day"]) - timedelta(days=365)
        except ValueError:
            return None
        lo = centre - timedelta(days=HEALTH_BASELINE_WINDOW
                                + HEALTH_YOY_TOLERANCE_DAYS)
        hi = centre + timedelta(days=HEALTH_YOY_TOLERANCE_DAYS)
        prev = []
        for r in self.history:
            if not r.get("qualifying"):
                continue
            try:
                d = date.fromisoformat(r["day"])
            except ValueError:
                continue
            if lo <= d <= hi and r.get("eta") is not None:
                prev.append(float(r["eta"]))
        if len(prev) < HEALTH_BASELINE_MIN:
            return None
        return round(mu_eta_now - sum(prev) / len(prev), 4)

    # ── Confidence (data certainty, NOT health level) ─────────────────────

    def confidence(self) -> dict[str, Any]:
        """0-100 % certainty that the health index is signal, not noise.

        confidence = 100 × maturity × Σ wᵢ·componentᵢ, with:
          maturity    n_qualifying / HEALTH_BASELINE_WINDOW   (hard gate: an
                      immature baseline caps everything else)
          resolution  median Δ23009 vs the 1 kWh counter quantisation
          yield       qualifying-day fraction of the closed days in the last
                      HEALTH_CONF_YIELD_WINDOW days (sparse ⇒ stale index)
          sensor      1 − mean sensor-suspect fraction over the window
          condition   Σ conditioning: |ρ| → 1 makes Σ⁻¹ (and thus T²)
                      numerically unstable; scored down from |ρ| = 0.9
        A white-noise-grade index therefore reads LOW confidence even while
        the health status itself may read "normal".
        """
        win = self._qualifying_window()
        n = len(win)
        maturity = min(1.0, n / HEALTH_BASELINE_WINDOW)

        if n:
            elec = sorted(float(r["elec_kwh"]) for r in win
                          if r.get("elec_kwh") is not None)
            med_elec = _percentile(elec, 0.5) or 0.0
            resolution = max(0.0, min(1.0, med_elec / HEALTH_CONF_ELEC_FULL_KWH))
            sensor = max(0.0, min(1.0, 1.0 - (
                sum(float(r.get("suspect_frac") or 0.0) for r in win) / n)))
        else:
            med_elec = 0.0
            resolution = 0.0
            sensor = 0.0

        recent = self.history[-HEALTH_CONF_YIELD_WINDOW:]
        yield_frac = (sum(1 for r in recent if r.get("qualifying"))
                      / len(recent)) if recent else 0.0

        rho = abs(float(self.latest.get("rho") or 0.0))
        condition = max(0.0, min(1.0, (0.99 - rho) / 0.09)) if rho > 0.9 else 1.0

        weighted = (HEALTH_CONF_W_RESOLUTION * resolution
                    + HEALTH_CONF_W_YIELD * yield_frac
                    + HEALTH_CONF_W_SENSOR * sensor
                    + HEALTH_CONF_W_CONDITION * condition)
        return {
            "confidence": round(100.0 * maturity * weighted, 1),
            "maturity": round(maturity, 3),
            "resolution": round(resolution, 3),
            "median_daily_elec_kwh": round(med_elec, 1),
            "yield": round(yield_frac, 3),
            "sensor_consistency": round(sensor, 3),
            "conditioning": round(condition, 3),
            "baseline_n": n,
        }

    # ── Persistence ───────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "history": self.history,
            "acc": self._acc.to_dict() if self._acc is not None else None,
            "last_ts": self._last_ts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> HealthModel:
        model = cls()
        hist = d.get("history")
        if isinstance(hist, list):
            model.history = [r for r in hist if isinstance(r, dict)]
            model.history = model.history[-HEALTH_HISTORY_MAX_DAYS:]
        acc = d.get("acc")
        if isinstance(acc, dict) and acc.get("day"):
            model._acc = _DayAccumulator.from_dict(acc)
        ts = d.get("last_ts")
        model._last_ts = float(ts) if isinstance(ts, (int, float)) else None
        model._recompute()
        return model


class HealthTracker:
    """HA glue: samples the coordinator every 5 minutes, feeds HealthModel,
    persists it, and dispatches health_signal on every processed sample."""

    def __init__(self, hass: HomeAssistant, entry, coordinator) -> None:
        self.hass = hass
        self._entry = entry
        self._coord = coordinator
        self.model = HealthModel()
        self._store: Store = Store(
            hass, STORAGE_VERSION,
            f"{DOMAIN}_{entry.entry_id}_{HEALTH_STORE_SUFFIX}",
        )
        self._unsub = None
        self._start_mono: float = 0.0   # v0.3.3 — settle-window reference
        self._blind_logged = False

    async def async_start(self) -> None:
        try:
            stored = await self._store.async_load()
            if isinstance(stored, dict):
                self.model = HealthModel.from_dict(stored)
                _LOGGER.debug(
                    "Hoval CAN health: restored %d day record(s)",
                    len(self.model.history),
                )
        except Exception as err:  # noqa: BLE001 — never block startup
            _LOGGER.warning(
                "Hoval CAN health: could not load stored history (%s) — "
                "starting fresh", err,
            )
        self._start_mono = time.monotonic()
        self._unsub = async_track_time_interval(
            self.hass, self._tick,
            timedelta(seconds=HEALTH_SAMPLE_INTERVAL_S),
        )

    async def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        try:
            await self._store.async_save(self.model.to_dict())
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Hoval CAN health: final save failed (%s)", err)

    @callback
    def _tick(self, now: datetime) -> None:
        # No connection → no sample. The per-day interval cap in the model
        # guarantees the gap is not integrated when data resumes.
        if not self._coord.connected:
            return
        # v0.3.3 cold-start readiness. Values seeded from the Store after a
        # restart are indistinguishable from live ones via get_value(), so
        # ask the coordinator which are still stale and refuse to integrate
        # those intervals. See const.py "Cold-start readiness" for why the
        # two tiers exist (a pure freshness gate deadlocks on a broadcast-
        # on-change bus; a pure timer resumes fabricating data once it
        # expires).
        is_restored = self._coord.is_restored
        blind = any(is_restored(d) for d in HEALTH_FRESH_REQUIRED)
        if not blind and (time.monotonic() - self._start_mono) < HEALTH_SETTLE_S:
            blind = any(is_restored(d) for d in HEALTH_SETTLE_REQUIRED)
        if blind and not self._blind_logged:
            self._blind_logged = True
            _LOGGER.debug(
                "Hoval CAN health: sampling blind — awaiting live frames for "
                "restored datapoint(s); intervals recorded as unobserved"
            )
        elif not blind and self._blind_logged:
            self._blind_logged = False
            _LOGGER.debug("Hoval CAN health: inputs live, sampling resumed")

        local = now.astimezone()
        get = self._coord.get_value
        raw_prog = get(DP_HEATING_PROGRAM)
        sample = Sample(
            ts=local.timestamp(),
            local_date=local.date().isoformat(),
            status_dhw=get(DP_STATUS_WW),
            status_hc=get(DP_STATUS_HC),
            modulation=get(DP_MODULATION),
            program=raw_prog if isinstance(raw_prog, str) else None,
            flow_c=get(DP_FLOW_TEMP),
            heat_gen_c=get(DP_HEAT_GEN),
            thermal_kw=get(DP_THERMAL_POWER),
            elec_mwh=get(DP_WEZ_ELEC_TOTAL),
            cycles=get(DP_WEZ_CYCLES),
            source_c=self._coord.source_temp_c,
            heater_on=self._coord.electric_heater_on,
            blind=blind,
            elec_fresh=not is_restored(DP_WEZ_ELEC_TOTAL),
            cycles_fresh=not is_restored(DP_WEZ_CYCLES),
        )
        self.model.add_sample(sample)
        self._store.async_delay_save(self.model.to_dict, PERSIST_SAVE_DELAY_S)
        async_dispatcher_send(self.hass, health_signal(self._entry.entry_id))

    def snapshot(self) -> dict[str, Any]:
        """Health block for downloadable diagnostics."""
        return {
            "latest": dict(self.model.latest),
            "confidence": self.model.confidence(),
            "history_days": len(self.model.history),
            "sampling_blind": self._blind_logged,
            "open_day_unknown_s": (
                round(self.model._acc.unknown_s, 1)
                if self.model._acc is not None else None
            ),
            "recent_days": self.model.history[-14:],
        }
