# Hoval CAN — Audit, ICS Deployment Quality (v0.3.1)

Scope: COP-model refinement (approach-temperature term, blended regime
transition, source-temperature default), energy-integrator re-sampling, and
the new `cop_approach_k_c` option. The integration remains strictly
**read-only** (no write path touched). Startup / restart-persistence logic is
**deliberately untouched** (owner decision) — verified unchanged, see below.
Every change is exercised by executing the real code
(`tests/test_protocol.py`, runnable without Home Assistant).

## Motivation

Field data from this installation (UltraSource T comfort (13), 200 m
borehole) exposed three accuracy limits in the v0.3.0 power model:

1. **Source temperature**: the 12.5 °C default was an initial estimate; the
   analog Erdsonde gauge read **16.5 °C during an active DHW charge**
   (July 2026). The passively-recharged deep borehole should stay ~15 °C
   even in winter, so v0.3.0 systematically over-estimated the lift and
   therefore electrical consumption (~10 % DHW, ~20 % SH at typical points).
2. **Bare-lift divergence**: with a 16.5 °C source and floor-heating flow
   temperatures (lift 11–15 K), `cop_base × ref/lift` computes 8.6–10.5 and
   pins at the 8.5 clamp — the model degenerated into a hardcoded constant
   for most of the heating season.
3. **Frozen-COP integration**: CAN broadcasts only on change. The energy
   integrators subscribed to thermal power (29051) alone, so a
   constant-thermal DHW plateau was integrated end-to-end at the COP frozen
   at the start of the plateau, while T_gen climbed ~40→52 °C and the true
   COP fell ~3.7→3.0 (10–20 % under-count on those spans). The v0.3.0
   60-second timer only refreshed the *display* at the stale rate.

## Changes

### Formula (const.py :: calculate_cop)
- **Approach term k** (new option `cop_approach_k_c`, default 7.0 °C, range
  0–15): lift correction becomes `(ref_lift + k) / (lift + k)`. k is added
  to numerator AND denominator, so the calibrated anchors (lift 17.5 SH /
  39.5 DHW) are **k-invariant**, and `k = 0` reproduces the pre-v0.3.1
  formula bit-for-bit (both proven by test). Physically: the refrigerant
  works across the heat-exchanger approach temperatures in addition to the
  water-side lift, so real COP saturates at small lifts instead of
  diverging. Negative k inputs are coerced to 0 inside the function; the
  coordinator property additionally clamps to [0, 15]; the options schema
  enforces the same range.
- **Blended regime transition**: pure SH ≤ 38 °C, pure DHW ≥ 42 °C, linear
  blend between — removes the ~16–19 % electrical-power step the hard 40 °C
  split produced mid-DHW-charge. Energy-neutral to within the blend window;
  continuity and monotonicity proven by test.
- `_cop_base_sh` / `_cop_base_dhw` factored out **unchanged** (coefficients
  identical; piecewise continuity at m = 12/22/33/60 unchanged). NOT
  refitted — deliberately, see "Reviewed, acceptable".
- `DEFAULT_SOURCE_TEMP_C` / `COP_SOURCE_TEMP` 12.5 → 16.5 °C (measured; the
  two constants are now kept in lockstep). An option value the user already
  saved is unaffected — defaults only apply where the key is absent.

### Energy integrators (sensor.py)
- `HovalHeatPumpElecEnergySensor` and `HovalTotalElecEnergySensor` now
  subscribe to **all COP inputs** — 29051 (thermal), 20052 (modulation),
  7 (T_gen) — matching what the Power sensors already did.
- Both now have a 60-second timer that **commits** the open interval at
  freshly recomputed values (the Total's timer previously only refreshed the
  display; the HP integrator had no timer). COP/state staleness in the
  integral is now capped at one minute regardless of CAN broadcast gaps.
- Timer guards: (a) no-op while disconnected — the timer can never undo the
  `_on_conn` interval discard, so downtime is still never integrated;
  (b) **continue-only** — the timer never opens an interval; only a
  dispatcher signal may arm the integrator (identical to pre-v0.3.1
  semantics; see defect register #1).
- Integration arithmetic itself unchanged: left Riemann, start-of-interval
  values, monotonic clock, `max(0, Δt)` guard, 3-decimal rounding,
  TOTAL_INCREASING, RestoreEntity. `unique_id`/`entity_id` unchanged.

### Options / UI
- New field **COP Approach Temperature k** in the options flow (validated
  schema, same pattern as the existing six), strings.json and
  translations/en.json extended (kept byte-identical to each other,
  verified). All 7 schema keys match the 7 UI keys (verified
  programmatically). Missing-key reads use `options.get(..., default)`
  everywhere, so existing entries upgrade with no migration.
- Source-temperature option description updated (which gauge to read, when).

## Expected numerical impact (defaults vs. v0.3.0 defaults)

| Operating point | COP old → new | Electrical |
|---|---|---|
| SH T_gen 26 °C | 8.5 → 8.5 (both clamp) | 0 % |
| SH T_gen 30 °C | 6.64 → 7.94 | −16 % |
| SH T_gen 35 °C | 5.17 → 6.39 | −19 % |
| DHW T_gen 45 °C | 3.68 → 3.97 | −7 % |
| DHW T_gen 52 °C | 3.03 → 3.31 | −9 % |

Weekly totals: ≈ −8 % DHW-dominated, ≈ −13…−15 % SH-dominated. Direction and
magnitude to be confirmed against the DpId 23009 hardware counter; k is the
calibration knob (±1 °C ≈ ∓2–3 % SH, ∓1 % DHW electricity).

## Defect register (found during this audit)

| # | Sev | Component | Finding | Resolution |
|---|-----|-----------|---------|------------|
| 1 | **P1** (pre-release) | HP energy `_tick` | First implementation of the committing timer could **arm** the integrator from coordinator data when tracking was cleared — e.g. after a reconnect, before any fresh 29051 broadcast, coordinator `_data` still holds stale pre-disconnect values, so the timer would have opened an interval at a stale rate and integrated phantom energy during a reconnected-but-quiet period. Behaviour did not exist in v0.3.0 (integrators re-armed only via signals). | Continue-only guard (`_last_ts is None → return`), mirroring the Total sensor's pre-existing guard. Regression test added ("tick does not arm from cleared tracking"). Caught in self-audit before release. |

## Reviewed, acceptable (no change)

- **Startup / restart persistence untouched** (owner decision): diffed —
  `_async_load_persisted`, `_schedule_persist`, `async_replay_restored_signals`,
  `PERSISTENT_DPIDS`, `__init__.py` ordering all byte-identical to v0.3.0.
  Entities' initial/Unknown/zero-fill semantics unchanged; replayed signals
  drive the new subscriptions exactly as they drove the old ones. The
  continue-only timer guards mean v0.3.1 adds **no new arming path** at
  startup.
- **Known stale-restore window remains** (documented as Known Gap #9 in
  CLAUDE.md): a restored nonzero thermal/modulation can integrate phantom
  compressor energy after a restart-while-stopped until the next on-change
  CAN frame. The committing tick does *not* widen this (it re-reads the same
  values, and cannot arm); deliberately out of scope per owner decision.
  Mitigation path documented for the future (live-confirmation gate).
- **`_cop_base_*` coefficients not refitted**: calibrated from data where
  modulation and lift are correlated; refitting before k and source_temp are
  settled against the 23009 counter would fit noise. Recorded as Known Gap
  #8 (calibration procedure) in CLAUDE.md.
- **Recorder churn**: the committing timers write state at most once per
  60 s and only when the rounded value changed (`write_always=False` path) —
  no new write amplification.
- **Blend cost**: both regime values computed on every call (previously
  one). Two extra multiplications at CAN event rate — negligible.
- **README verification template** rewritten to the new formula (k + blend,
  new defaults) so the optional cross-check cannot silently drift; ordering
  round→clamp now exactly matches the Python implementation.

## Tests

`python3 tests/test_protocol.py` — 12 groups, **ALL PASS**; `pyflakes` clean
across all modules and the test file; all four JSON files parse;
strings.json ≡ translations/en.json.

New/updated coverage:
- `test_cop` rewritten: anchor pins at explicit src = 12.5 (values identical
  to v0.3.0), anchor k-invariance (k = 0 and k = 15), k = 0 legacy
  equivalence off-anchor (SH and DHW), k > 0 monotone effect, blend edge
  continuity + centre value + strict monotonicity, new-default operating
  points (src 16.5 / k 7), guards (m ≤ 1, t ≤ source), clamp at tiny lift,
  negative-k coercion.
- `test_integrator_resample` (new): COP change mid-constant-thermal plateau
  is integrated piecewise (recovers the frozen-COP under-count), None
  resets without integrating, tick no-op while disconnected, tick
  continue-only for BOTH integrators, `_current_kw` live recompute
  (with/without heater), tick-commit re-arms at the recomputed rate.
- `test_power_model_options` extended: approach_k default / override /
  garbage / negative-clamp / max-clamp; coordinator `cop` consumes both
  options (k = 0 vs k = 7 pinned values); source-temp default 16.5.
