# Hoval CAN — Audit, ICS Deployment Quality (v0.3.2)

Scope: the new heat-pump health index (health.py, three entities, health
persistence store, diagnostics block), the correction of the health
specification's circular-COP design flaw, one persistence-set extension
(DpId 2080), and three behaviour-neutral hot-path optimisations. The
integration remains strictly **read-only** (no write path touched; the
health model only *reads* coordinator state). Startup / restart-persistence
logic for the existing sensors is **unchanged** — the health tracker uses
its own, separate Store. Every claim below is exercised by executing the
real code: `tests/test_protocol.py` (154 checks incl. the full unchanged
v0.3.1 regression set) and `tests/test_health.py` (62 checks), both
runnable without Home Assistant.

## Motivation

The "Hoval Heat Pump Health Index" specification (reviewed prior to this
release) is statistically sound in its architecture — self-referential
baselines, mode gating, purity days, Hotelling-T² fusion of cycling rate
and Gütegrad — but contained one **critical classification error**: it
listed "Heat Pump COP" as device telemetry. On this integration that
sensor is *synthetic*: `calculate_cop()` maps modulation and temperatures
through a calibrated piecewise model with a configurable calibration knob
(approach-k). It contains **no measured electrical quantity**. A Gütegrad
η = COP/COP_carnot built on it divides one temperature model by another:
if the compressor degrades, modulation and temperatures can hold their
values while real electricity consumption rises — and the "health" index
would not move. Worse, recalibrating k (which the README explicitly
instructs the owner to do against the hardware counter) would *shift the
health baseline* without any physical change to the machine. This also
violates the spec's own Principle 2 ("measured data only").

## Model provenance — corrections applied vs. the original spec

| Spec element | Assessment | v0.3.2 implementation |
|---|---|---|
| η from live COP sensor | **Rejected — circular** (see above) | η(day) = PF(day) / mean COP_carnot(day); PF = ∫ DpId 29051 dt ÷ Δ DpId 23009. Both factors measured; hardware counter is the ground truth the COP model itself is calibrated against |
| CycleRate from DpId 2080 + purity days | Sound — 2080 is untagged, purity attribution is the only honest method | Implemented as specified; purity bound 5 % of *observed* time, using real status datapoints |
| Passive-cooling detection via cooling-energy entity | **Rejected** — that entity is a configured plug estimate (principle-2 violation) | DpId 2051 == 9 (real CAN status) |
| DHW detection | — | DpId 2052 == 8 (real CAN status); a DHW-charging compressor is never misattributed to SH |
| T_sink for Carnot | Spec left it open | Flow temperature (DpId 2), with heat-generator temperature (DpId 7) as an independent cross-check witness: divergence > 3 °C ⇒ sample sensor-suspect, excluded from the Carnot mean, counted toward day rejection |
| T_source constant | Spec assumed fixed 16.5 °C | Reads the live `source_temp_c` option — per the integration's own README this is a *seasonal, manually adjusted* value; hardcoding the summer reading would bias every winter Carnot term |
| Empirical 99th percentile as "high" | **Rejected** — at n ≈ 90 the 99th percentile interpolates the top two order statistics; it is noise | Parametric Hotelling limit: T²_lim = [2(n+1)(n−1)/(n(n−2))]·F_q(2, n−2), with the F quantile in closed form (numerator df = 2 inverts exactly: (d/2)((1−q)^(−2/d) − 1)). No SciPy dependency. Verified against χ²₂/2 convergence and an exact finite-d point |
| Rolling 90-day baseline only | Incomplete — an adaptive baseline *tracks and therefore hides* slow multi-month degradation | Added `eta_yoy_delta`: season-matched year-over-year mean-η delta (365-day offset, ±21-day tolerance, ≥30 prior-season days required). The deliberately non-adaptive anchor |
| ≥5-day persistence rule | Sound | Implemented (`sustained_alert`), plus the self-masking guard below |
| Confidence concept (owner request) | New in v0.3.2 | Separate entity quantifying certainty of the *data*, never the health level |

### Defect found by test, not by review: percentile self-masking

The empirical 95th-percentile "elevated" threshold is computed over the
trailing window — which *contains the days being judged*. The first
implementation failed its own sustained-fault test: five identical
elevated days lifted the window's 95th percentile onto themselves and the
alert never fired. Fix: the percentile reference pool excludes the
trailing `HEALTH_ALERT_RUN_DAYS` (5) days whenever ≥ 30 days remain. This
is exactly the failure mode a paper review does not catch and an executed
test does — it is why the test suite simulates faults end-to-end.

### Calibration decision: HEALTH_ETA_PLAUSIBLE = (0.08, 0.85)

Literature Gütegrad values (0.4–0.6) reference the refrigerant-side lift.
This model's η divides a **whole-unit** daily PF (pumps and standby sit
inside Δ23009; on purity days DHW residue is bounded by the purity rule)
by the pure **water-side** Carnot COP — which at this installation's
floor-heating lift (~13.5 K → COP_carnot ≈ 22) puts *healthy* days near
η ≈ 0.18. The end-to-end simulation showed a 25 % efficiency loss lands
at ≈ 0.13: with the initially chosen 0.15 floor, the model would have
**rejected as implausible the very days it exists to flag**. The floor was
lowered to 0.08 so genuine degradation stays inside the model, while
pipeline-grade nonsense (PF < 2 at this lift) is still excluded. The band
is a data-validity gate, not a health judgment.

## Changes

### New module: health.py
Two strictly separated layers:

- **HealthModel** — pure Python; no HA imports, no clocks, no I/O; fully
  serialisable. All statistics implemented directly (2×2 Σ inverted
  analytically; ridge ε = 0.01 when det < ε; σ floored at 1e−6 — no
  division by zero on degenerate baselines).
- **HealthTracker** — the only HA-aware part: a 5-minute
  `async_track_time_interval` tick reads the coordinator, feeds a
  `Sample`, debounce-saves (30 s, same policy as the existing coordinator
  store) to its **own** Store key (`hoval_can_{entry}_health`), and
  dispatches `health_signal`. The tick is a **no-op while disconnected**.

### Entities (sensor.py)
`Health Index T2` (state = T², all model internals as attributes),
`Health Status` (ENUM: normal / elevated / high / insufficient_baseline /
insufficient_mode_data; last day's reject reasons as attributes),
`Health Confidence` (%; component breakdown as attributes). All push-based
on `health_signal`; available whenever the tracker exists — they render
stored statistics and deliberately do **not** go unavailable with the TCP
link (same policy as the diagnostic sensors: surfacing state is the point).

### Confidence metric (owner requirement: "white noise must read low")
`confidence = 100 × maturity × (0.30·resolution + 0.30·yield +
0.20·sensor_consistency + 0.20·conditioning)` where maturity = n/90
(hard multiplicative gate), resolution = median daily Δ23009 vs. the 1 kWh
quantisation (full at ≥ 20 kWh), yield = qualifying fraction of the last
14 closed days, sensor_consistency = 1 − mean suspect fraction, and
conditioning penalises |ρ| > 0.9 (near-singular Σ makes T² numerically
meaningless). Proven monotone in every component by test: worse data can
only lower it. It measures certainty of the *index*, never health.

### Wiring
- `__init__.py`: tracker created and attached as
  `coordinator.health_tracker` **before** the sensor platform is
  forwarded (entities always find it); stopped — with a final save —
  in `async_unload_entry` before the coordinator stops.
- `const.py`: one `HEALTH_*` constants block with design-invariant
  comments; `health_signal()`; DpId names for flow temp / cycles /
  program; **DP_WEZ_CYCLES (2080) added to PERSISTENT_DPIDS** so the
  health sampler has a cycles value immediately after a restart instead
  of waiting for the next CAN broadcast of an unchanged counter. This
  reuses the existing, unmodified persistence machinery; the only effect
  on pre-existing behaviour is one more dp_id in the stored snapshot and
  replay (additive; verified by the unchanged persistence tests).
- `diagnostics.py`: new "health" block (latest statistics, confidence
  breakdown, last 14 day-records) — a surprising T² is auditable from the
  diagnostics download alone.

### Optimisations (behaviour-neutral by construction and by proof)
1. **Memoised dispatcher-signal strings** (`_dp_signal` cache + the three
   derived-state signals precomputed in `__init__`): `_update_dp` runs for
   every decoded frame; it no longer rebuilds f-strings per frame.
2. **Precomputed BE value-length table** (`_BE_VLEN`, module level): the
   frame parser's per-frame `SENSOR_BY_DPID.get → dataclass attr →
   TBYTES.get` chain replaced by one dict lookup. STR and unmapped dp_ids
   are simply absent → `.get()` returns None, matching the previous
   branch chain exactly (including the end-marker-scan fallback).
3. **Generator-based rate windows**: the two rate properties no longer
   materialise the snapshot deque into a list per read.

Proof of neutrality: the complete v0.3.1 test suite — including the
adversarial framing set (in-value FF 01/FF 02, desync/resync,
length-vs-marker mismatch), watchdogs, integrators, persistence
round-trip, diagnostics and rates — runs unmodified against the optimised
coordinator and passes. No public API, signal name, timing constant, or
parsing decision changed.

## Reviewed, acceptable (deliberate non-changes)

- **No new config options.** The model is self-referential by design;
  every constant is a documented `HEALTH_*` value in const.py. Exposing
  thresholds as options would invite exactly the fabricated-absolute-
  threshold failure the spec forbids.
- **Purity days are scarce by construction** (~50–70 % of winter days
  qualify; zero in summer). Accepted: a slower honest index beats a fast
  contaminated one. The scarcity is *visible* (yield component of
  confidence; reject reasons on the status entity) rather than hidden.
- **Whole-unit PF** (pumps + standby inside Δ23009) rather than
  compressor-only: no measured compressor-only electrical datapoint
  exists. The constant offset is absorbed by the self-referential
  baseline; only day-to-day *variation* matters. Documented in README.
- **The trailing-90-qualifying-day window straddles the summer gap** at
  season start (≈30 old-season days mixed in). Accepted deliberately:
  autumn gets an immediate baseline instead of a 30-day blind restart;
  the weekly-adapting window flushes the old season within a month, and
  the YoY anchor is the instrument for cross-season comparison anyway.
- **Stale-restore integration window (known gap #9)** remains untouched
  per the standing owner decision; the health model is additionally
  defended against it by its own gap cap (below).
- **`calculate_cop()` and every existing sensor untouched.** The health
  model runs beside, not inside, the power model.

## Failure-mode analysis (ICS lens)

| Failure | Behaviour | Mechanism / test |
|---|---|---|
| HA restart mid-day | Open day survives; no phantom time/energy | Own Store; `last_ts` persisted; first post-restore interval capped by HEALTH_MAX_GAP_S = 900 s → gap contributes 0 s / 0 kWh. Tested (tracker restart round-trip; gap-cap unit test) |
| Gateway outage | No samples taken at all | Tracker tick no-ops while disconnected. Tested |
| Hardware counter reset / device swap | Whole day rejected (`counter_reset`), never clamped or wrapped | First/last endpoint comparison. Tested |
| Δ23009 below quantisation | Day rejected (`elec_delta_too_small`), PF never divided into noise | 5 kWh floor vs. 1 kWh resolution. Tested |
| Electric heater fires | Day rejected (`electric_heater_active`) — Heizstab kWh would corrupt PF | Reuses the audited heater detection. Tested |
| Temperature sensor drift/fault | Sample suspect (cross-check), day rejected at > 50 % suspect; η outside (0.08, 0.85) rejected with value still recorded | Suspect-the-pipeline-first rule. Tested |
| Near-singular Σ (features co-move) | Ridge ε = 0.01; T² finite; conditioning component of confidence drops | Tested (perfectly collinear window) |
| Sustained real fault | Cannot lift its own threshold (self-masking guard); `sustained_alert` after ≥ 5 days | Tested (identical-value run + end-to-end injected degradation → elevated/high) |
| Summer / long idle | `insufficient_mode_data`, not a fake "normal"; confidence yield falls | Tested |
| Corrupt health store | Warn + start fresh; never blocks startup (same policy as the coordinator store) | Tested (`from_dict` on garbage) |
| Clock steps | Negative inter-sample intervals discarded (cap check is two-sided) | Code path shared with the gap cap |

**Resource bounds:** history hard-capped at 400 day-records (~a few hundred
bytes each, JSON); one open-day accumulator; recompute is O(window) = O(90)
once per day close; the 5-minute tick is O(1) dict reads. One debounced
store write per tick window. No unbounded growth anywhere.

**Read-only property:** unchanged. health.py contains no writer, no socket,
no frame construction; its only inputs are `coordinator.get_value()` /
derived properties.

## Test evidence

`python3 tests/test_protocol.py` → ALL PASS (v0.3.1 regression set
unchanged + new health-tracker glue group: tick→sample→store→signal,
disconnected no-op, tracker restart round-trip; HA stub extended only by
`SensorDeviceClass.ENUM`).

`python3 tests/test_health.py` → ALL PASS. Highlights: F(2,d) quantile
verified against χ²₂/2 convergence (2.996 / 4.605) and the exact
F(0.95, 2, 10) = 4.103; thermal integration exact to 0.1 kWh over a
simulated day; Carnot mean exact to 0.01; every rejection path exercised;
6σ decoupled excursion → `high` with correct z signs; a **45-day
end-to-end simulation at 5-minute cadence with a 1 kWh-quantised
electrical counter** builds a ≥ 30-day baseline, reads `normal`, then an
injected degradation day (cycles ×5, electricity ×1.8) qualifies and is
flagged `elevated`/`high`; YoY delta reproduces the hand-computed value;
confidence is monotone under every data-quality degradation and gates
hard on immaturity; persistence round-trips including the open day.

## Field expectations (this installation, installed July 2026)

Status will read `insufficient_mode_data` / `insufficient_baseline`
through the summer; qualifying days begin with the heating season; the
first T² value is expected around **November** (30th qualifying day), with
confidence climbing as the window fills toward 90 days mid-winter. The
first YoY anchor value requires a second heating season (winter 2027/28).
No dashboard action needed before then — the status and confidence
entities are informative from day one.
