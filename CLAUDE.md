# CLAUDE.md — Hoval CAN Integration Developer Context

Version 0.3.2. Local-push HA integration for Hoval heat pumps via WLAN Gateway.
Read-only. TCP port 3113, proprietary CAN-BUS stream.
Installation: Hoval UltraSource T comfort (13), 2020, R410A, B0/W35 13.3 kW,
200 m borehole (analog Erdsonde gauges only — no CAN datapoint for brine temp).

---

## Frame protocol (reverse-engineered, May–June 2026)

```
[FF 01] [3B hdr] [2B unit_id BE] [1B cmd] [2B group_id BE] [2B dp_id BE] [value] [FF 02]
```

| Cmd  | Name | Action |
|------|------|--------|
| 0x42 | READ-RESP BE | Decode — big-endian value |
| 0x62 | READ-RESP LE | Decode — little-endian from room display (skip byte 10, decode bytes 11-12) |
| others | — | Ignore |

0x62 only carries data when `group_id & 0x8000`.
Schedule groups 15614/15922/15923/15924 skipped. DpIds 64000-64050 skipped.

TypeName bytes: U8/S8=1, U16/S16/LIST=2, U32/S32=4, S64=8.
Null sentinels: U16=0x8000, S16=−32768, U32=0x80000000/0xFFFFFFFF, S32=−2147483648, any=all-FF.

---

## Key DatapointIds

| DpId | Key | Entity | Type | Notes |
|------|-----|--------|------|-------|
| 7 | heat_gen_temp | `sensor.hoval_can_heat_generator_temperature` | S16 dec=1 °C | **COP input**; persistent (v0.3.0) |
| 20052 | compressor_modulation | `sensor.hoval_can_compressor_modulation` | U8 % | **COP input**; drives `heat_pump_active`; persistent (v0.3.0) |
| 29051 | current_heating_power | `sensor.hoval_can_current_heating_power` | U32 dec=1 kW | Energy integration; persistent (v0.3.0) |
| 2052 | status_dhw | `sensor.hoval_can_status_dhw` | U8 | 8=charging → heater detection; persistent (v0.3.0) |
| 2051 | status_heating_circuit | `sensor.hoval_can_status_heating_circuit` | U8 | 9=passive cooling → `passive_cooling_on`; persistent (v0.3.0) |
| 2053 | status_heat_pump | `sensor.hoval_can_status_heat_pump` | U8 | Decoded, persistent (v0.3.0); not yet consumed by a derived property |
| 4 | dhw_temp | `sensor.hoval_can_dhw_temp` | S16 dec=1 °C | Heater detection; persistent (v0.3.0) |
| 1004 | dhw_setpoint | `sensor.hoval_can_dhw_setpoint` | S16 dec=1 °C | Heater detection; persistent (v0.3.0) |
| 23009 | total_wez_electrical_energy | … | U32 dec=3 MWh | Hardware counter; persistent; **health PF denominator (v0.3.2)** — 1 kWh quantisation |
| 2080 | wez_switch_cycles | `sensor.hoval_can_wez_switch_cycles` | U32 counter | **Health CycleRate input (v0.3.2)**; added to PERSISTENT_DPIDS in v0.3.2 |
| 2 | flow_temp | `sensor.hoval_can_flow_temperature` | S16 dec=1 °C | **Health T_sink (v0.3.2)** — Carnot term; DpId 7 is the cross-check witness |
| 502 | active_heating_program | `sensor.hoval_can_active_heating_program` | STR | **Health mode gate (v0.3.2)** — "Sommer"/"Standby" excludes SPACE_HEATING_ACTIVE |

"Persistent (v0.3.0)" = in `PERSISTENT_DPIDS`: both entity-level restore
(`HovalPersistentSensor`) AND seeded into `coordinator._data` via the Store
before the sensor platform loads — see "Restart persistence" below.

Modbus register ≠ DatapointId (different address spaces). E.g. Modbus reg 19484 → DpId 9058.

---

## Dynamic COP (const.py :: calculate_cop)

### Formula (v0.3.1: approach-k + blended regimes)
```python
def calculate_cop(modulation, heat_gen_temp,
                  source_temp=COP_SOURCE_TEMP,          # option default 16.5
                  approach_k=DEFAULT_APPROACH_K_C):     # option default 7.0
    if modulation <= 1.0 or heat_gen_temp <= source_temp:
        return 0.0
    k = max(0.0, approach_k)
    lift_eff = (heat_gen_temp - source_temp) + k
    cop_sh  = _cop_base_sh(modulation)  * ((17.5 + k) / lift_eff)
    cop_dhw = _cop_base_dhw(modulation) * ((39.5 + k) / lift_eff)
    if heat_gen_temp <= 38.0:   cop = cop_sh
    elif heat_gen_temp >= 42.0: cop = cop_dhw
    else:                       # linear blend, weight (t-38)/4 toward DHW
        w = (heat_gen_temp - 38.0) / 4.0
        cop = (1 - w) * cop_sh + w * cop_dhw
    return max(1.0, min(8.5, round(cop, 4)))
```
`_cop_base_sh`: 0.5833·m (m<12) | 7.0 (12≤m≤22) | 7.988−0.0449·m (m>22)
`_cop_base_dhw`: 4.626−0.0417·m (m≤33) | 3.679−0.0130·m (≤60) | 3.500−0.0100·m

**Approach term k** (v0.3.1): added to reference AND actual lift →
calibration anchors (lift 17.5 SH / 39.5 DHW) are k-invariant; k=0
reproduces the pre-v0.3.1 bare-lift formula exactly. Physically: the
refrigerant works between ~T_source−approach and ~T_gen+approach, so real
COP saturates at small lifts instead of diverging. Without k, source 16.5 °C
+ floor-heating flow temps pin the COP at the 8.5 clamp all heating season.
**k is the calibration knob vs. the DpId 23009 hardware counter**:
±1 °C ≈ ∓2-3 % on SH electricity, ∓1 % on DHW.

**Blend 38-42 °C** (v0.3.1): removes the ~16-19 % electrical-power step the
hard 40 °C split produced mid-DHW-charge. Cosmetic for totals.

### Constants to recalibrate (all in const.py)
```python
COP_SOURCE_TEMP     = DEFAULT_SOURCE_TEMP_C  # 16.5 °C (v0.3.1 — measured on
                                             # the Erdsonde gauge during a DHW
                                             # charge, July 2026; ~15 expected
                                             # in winter). Fallback only —
                                             # normally the CONF_SOURCE_TEMP
                                             # option is passed in.
DEFAULT_APPROACH_K_C = 7.0   # CONF_APPROACH_K option default (0-15)
COP_SH_LIFT_REF     = 17.5   # °C — SH reference lift  (t_gen_ref=30 @ src 12.5)
COP_DHW_LIFT_REF    = 39.5   # °C — DHW reference lift (t_gen_ref=52 @ src 12.5)
COP_SH_MAX_TGEN     = 40.0   # °C — nominal split (blend centre)
COP_BLEND_LOW_TGEN  = 38.0   # °C — pure SH below
COP_BLEND_HIGH_TGEN = 42.0   # °C — pure DHW above
COP_CLAMP_MIN, COP_CLAMP_MAX = 1.0, 8.5
```
Piecewise coefficients live in `_cop_base_sh` / `_cop_base_dhw` — edit to
recalibrate. **Do NOT refit them before k and source_temp are settled** (they
were calibrated from data where modulation and lift are correlated; refitting
against uncorrected residuals fits noise).

### Validation points
At the anchors with explicit src=12.5 (k-invariant): SH t=30: m=12-22 → 7.0;
m=33 → 6.51; m=50 → 5.74. DHW t=52: m=33 → 3.25; m=100 → 2.50.
Legacy k=0 off-anchor: SH t=35 m=50 → 4.4668; DHW t=45 m=50 → 3.6814.
Defaults (src=16.5, k=7): SH t=30 m=30 → 7.9368; DHW t=52 m=50 → 3.3141.
Blend m=50: t=38 → 4.3293; t=40 → 4.0805; t=42 → 3.8589 (monotone).

### Entity subscriptions for COP-dependent sensors
| Sensor | Subscribes to |
|--------|--------------|
| HovalDynamicCOPSensor | dp_20052, dp_7 |
| HovalHeatPumpElecPowerSensor | dp_29051, dp_20052, dp_7 |
| HovalHeatPumpElecEnergySensor | dp_29051, dp_20052, dp_7 + 60 s committing tick (v0.3.1) |
| HovalBrinePumpPowerSensor | dp_20052, cooling_signal |
| HovalHeatingPumpPowerSensor | dp_20052, cooling_signal |
| HovalStandbyPowerSensor | connection_signal only (constant value) |
| HovalTotalElecPowerSensor | dp_29051, dp_20052, dp_7, heater_signal, cooling_signal |
| HovalTotalElecEnergySensor | dp_29051, dp_20052, dp_7, heater_signal, cooling_signal + 60 s committing tick (v0.3.1) |

---

## Electric heater detection (coordinator.electric_heater_on)

```python
heater_on = (status_ww == 8 and dhw < dhw_sp
             and heat_gen <= dhw + 5.0
             and modulation <= 1.0)   # DHW priority: running compressor = HP charging tank
```
Returns None until all four temp/status DpIds received. DHW-priority by design:
while charging, a running compressor is itself heating the tank, so the Heizstab
is off; it only finishes once the compressor stops. Recomputed on updates to
status_ww / dhw / dhw_sp / heat_gen / modulation.
Rated power from `entry.options["heater_power_kw"]` (default 3.0 kW).

---

## Pump/standby model (coordinator properties, v0.3.0)

```python
heat_pump_active  # bool|None: modulation > COMPRESSOR_RUNNING_MODULATION; None until dp_20052 seen
pumps_active      # bool|None: heat_pump_active OR passive_cooling_on; None only if BOTH unseen
brine_pump_kw     # options: CONF_BRINE_PUMP_POWER, default 30 W -> kW
heating_pump_kw   # options: CONF_HEATING_PUMP_POWER, default 20 W -> kW
standby_kw        # options: CONF_STANDBY_POWER, default 12 W -> kW (unconditional — never zero-filled)
```
Brine + heating pump share `pumps_active` because passive cooling circulates
the ground loop through the floor circuit with the compressor bypassed — both
pumps physically run in that mode too, not just during active heating/DHW.
This is why `cooling_power_kw` was retired from Total Electrical Power/Energy:
it modelled the same two pumps as one lump estimate; keeping both would
double-count during passive cooling.

---

## Health index (health.py, v0.3.2)

Daily self-referential FDD model. **Measured inputs only** — the synthetic
`calculate_cop()` is deliberately NOT an input (no measured electrical
quantity in it ⇒ any η built on it is circular; this was the flaw in the
original spec that v0.3.2 corrects).

```
CycleRate(day) = Δ dp2080                       (hardware cycles counter)
PF(day)        = ∫ dp29051 dt / (Δ dp23009 · 1000)   [kWh_th / kWh_el]
COP_carnot     = (flow°C+273.15)/(flow−t_source) per 5-min SH sample
η(day)         = PF / mean(COP_carnot)          ("Gütegrad", whole-unit)
T²             = z' Σ⁻¹ z  over trailing 90 qualifying days (min 30)
```

Two layers in `health.py`:
- `HealthModel` — pure Python, zero HA imports, fully serialisable
  (`to_dict`/`from_dict`), unit-tested standalone in `tests/test_health.py`.
  Owns: mode gate (`classify_mode` — DHW dp2052==8 wins > passive cooling
  dp2051==9 > SH needs modulation>1 & program not in
  HEALTH_EXCLUDED_PROGRAMS; unseen program never blocks), `_DayAccumulator`
  (left-Riemann thermal integration over compressor-running samples, gap cap
  HEALTH_MAX_GAP_S=900 s so restarts/outages never create energy, counter
  first/last endpoints + reset detection, flow-vs-heat-gen >3 °C ⇒ suspect),
  day qualification with explicit reject reasons, baseline → z → ridge-
  regularised 2×2 Σ → analytic T², statuses, sustained-alert run, YoY anchor,
  confidence metric.
- `HealthTracker` — HA glue: 5-min `async_track_time_interval` tick (no-op
  while disconnected), builds a `Sample` from the coordinator, own `Store`
  (`{DOMAIN}_{entry_id}_health`), debounced 30 s saves + final save on stop,
  dispatches `health_signal`. Created in `__init__.py` and attached as
  `coordinator.health_tracker` BEFORE the sensor platform is forwarded;
  stopped in `async_unload_entry` before the coordinator.

Key statistical decisions (rationale in AUDIT_v0.3.2.md):
- "elevated" = empirical 95th percentile of the window's own T², with the
  trailing HEALTH_ALERT_RUN_DAYS excluded from the percentile pool
  (**self-masking guard** — a sustained fault must not lift its own
  threshold; found by test).
- "high" = parametric Hotelling limit, closed-form F(2, n−2) quantile:
  `f_quantile_df1_2(q,d) = (d/2)((1−q)^(−2/d) − 1)` — no SciPy. The naive
  empirical 99th percentile at n≈90 is meaningless (interpolates the top
  two order statistics).
- HEALTH_ETA_PLAUSIBLE = (0.08, 0.85): whole-unit PF ÷ WATER-side Carnot at
  this installation's ~13.5 K lift ⇒ healthy η ≈ 0.18; a 25 % degradation
  (≈0.13) must stay INSIDE the band to be flagged rather than rejected.
  Calibrated by the end-to-end simulation in tests/test_health.py. Do not
  "fix" it back to the literature's 0.4–0.6 (refrigerant-side reference).
- `eta_yoy_delta`: season-matched year-over-year mean-η delta (365-day
  offset, ±21-day tolerance, needs ≥30 prior-season days) — the
  NON-adaptive anchor; the rolling baseline alone tracks-and-hides slow
  drift. Note: the trailing-90-*qualifying*-day window straddles the summer
  gap at season start (mixes ~30 old-season days) — deliberate, gives
  autumn an immediate baseline.
- Confidence (Health Confidence sensor) = 100 × maturity(n/90) ×
  (0.30·resolution + 0.30·yield + 0.20·sensor_consistency +
  0.20·conditioning). Data certainty, NOT health level.

Entities (sensor.py): `HovalHealthIndexSensor` (T² + full attrs),
`HovalHealthStatusSensor` (ENUM: normal/elevated/high/insufficient_baseline/
insufficient_mode_data), `HovalHealthConfidenceSensor` (%). All push on
`health_signal`, available whenever the tracker exists (independent of the
TCP connection — they render stored statistics). Diagnostics gained a
"health" block (latest, confidence, last 14 day-records).

---

## Restart persistence (coordinator.py, v0.3.0)

```python
self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}_state")
self._restored_dpids: set[int] = set()
```
- `async_start()` calls `_async_load_persisted()` **before** the read loop
  starts and **before** `__init__.py` forwards the sensor platform — seeds
  `self._data` from the store for every dp_id in `PERSISTENT_DPIDS`.
- `__init__.py` calls `coordinator.async_replay_restored_signals()`
  **after** `async_forward_entry_setups()` — fires `dp_signal` (+ `heater_signal`
  / `cooling_signal` as applicable) for each restored dp_id, now that entities
  are subscribed. This is the step that actually makes Total Electrical Power
  resolve to a real number immediately after a restart.
- `_update_dp()` schedules a debounced (`PERSIST_SAVE_DELAY_S` = 30 s) write
  via `Store.async_delay_save()` whenever a `PERSISTENT_DPIDS` member changes,
  and drops the dp_id from `_restored_dpids` (live data always wins).
- A corrupt/missing store logs a warning and returns — never blocks startup.


---

## Architecture

```
__init__.py      Setup; replays restored signals post-platform-setup;
                 options reload listener (preserves energy totals)
config_flow.py   ConfigFlow (IP+port); OptionsFlow (heater, cooling [legacy],
                 source_temp, approach_k [v0.3.1], brine_pump, heating_pump,
                 standby)
coordinator.py   TCP reader; frame parser; signals; Store-backed restart
                 persistence (v0.3.0); cop / heater_power_kw / source_temp_c /
                 approach_k_c (v0.3.1) / brine_pump_kw / heating_pump_kw /
                 standby_kw / heat_pump_active / pumps_active properties;
                 v0.3.2 hot-path caches (_dp_signal memo, _BE_VLEN table)
health.py        v0.3.2 — HealthModel (pure statistics) + HealthTracker
                 (5-min sampler, own Store, health_signal)
sensor.py        HovalSensor, HovalPersistentSensor, HovalDynamicCOPSensor,
                 HovalHealthIndexSensor/StatusSensor/ConfidenceSensor (v0.3.2),
                 HovalElectricHeaterPowerSensor, HovalElectricHeaterEnergySensor,
                 HovalPassiveCoolingPowerSensor/EnergySensor (legacy, v0.3.0),
                 HovalHeatPumpElecPowerSensor, HovalHeatPumpElecEnergySensor,
                 HovalBrinePumpPowerSensor, HovalHeatingPumpPowerSensor (v0.3.0),
                 HovalStandbyPowerSensor (v0.3.0),
                 HovalTotalElecPowerSensor, HovalTotalElecEnergySensor
binary_sensor.py HovalElectricHeaterBinarySensor
const.py         All constants; calculate_cop(source_temp, approach_k;
                 blended regimes — v0.3.1); _cop_base_sh/_cop_base_dhw;
                 sensor descriptions; PERSISTENT_DPIDS (extended v0.3.0);
                 STORAGE_VERSION / PERSIST_SAVE_DELAY_S (v0.3.0)
strings.json / translations/en.json   Options UI (7 fields as of v0.3.1)
```

## Dispatcher signals
- `hoval_can_{entry_id}_dp_{dp_id}` — DatapointId update
- `hoval_can_{entry_id}_electric_heater` — heater on/off change
- `hoval_can_{entry_id}_cooling` — passive-cooling on/off change (also used
  as the recompute trigger for `pumps_active`-dependent sensors, v0.3.0)
- `hoval_can_{entry_id}_connection` — TCP connected/disconnected
- `hoval_can_{entry_id}_health` — health model updated (every processed
  5-min sample; entities re-render state + attributes) (v0.3.2)

---

## Known gaps / future work
1. **0x70 PUSH-MULTI** — compact status blocks not decoded (skipped by the
   length-aware parser via end-marker scanning; never corrupts monitored dpids)
2. **LIST text labels** — DpIds 3050/9075 show integer codes
3. ~~**COP_SOURCE_TEMP** not yet in options flow~~ — done in v0.3.0
   (`CONF_SOURCE_TEMP`, still a manual seasonal estimate — no CAN datapoint
   reports ground-loop temperature on this installation)
4. **Winter heater capture** — detection logic correct but not validated live
5. **Write support** — 0x56 frames observed; format partially characterised
6. **Frame value-field offset** — the parser assumes the value sits at byte 10
   immediately before `FF 02` (per the documented layout). If the device ever
   emits trailing bytes before `FF 02`, the `framing_errors` counter rises and
   the data watchdog forces reconnects — i.e. the assumption is observable and
   fail-safe, not silent. Re-confirm against a live capture if `framing_errors`
   is non-trivial in the field.
7. **Brine pump wattage (30 W default, v0.3.0)** — Hoval's spec sheet confirms
   it's the same high-efficiency pump class as the heating-circuit pump, but
   the exact figure hasn't been independently measured on this installation;
   update `CONF_BRINE_PUMP_POWER` once confirmed.
8. **Approach-k calibration (v0.3.1)** — `CONF_APPROACH_K` (default 7.0 °C)
   is a physically-motivated estimate, not yet calibrated. Compare weekly
   deltas of Total Electrical Energy vs. the DpId 23009 hardware counter,
   ideally one DHW-dominated and one SH-dominated week: raise k if the
   integration over-reads, lower if it under-reads (±1 °C ≈ ∓2-3 % SH, ∓1 %
   DHW). Do not refit the `_cop_base_*` coefficients before k and
   source_temp are settled.
9. **Stale-restore integration window** — restored (persisted) nonzero
   `current_heating_power`/`compressor_modulation` can integrate phantom
   compressor energy after a restart if the compressor stopped while HA was
   down (CAN broadcasts on change; the stop frame was missed and won't
   repeat). v0.3.1's 60-s committing tick does NOT close this — it re-reads
   the same coordinator values. Deliberately not addressed (owner decision:
   keep startup/persistence logic untouched); bounded by the stochastic CAN
   retransmit interval. If addressed later: gate the integrators' compressor
   term on a live-confirmed flag rather than changing replay.
10. **status_heat_pump (DpId 2053)** is now decoded and persisted but not yet
   consumed by any derived property — `passive_cooling_on` still reads
   `status_heating_circuit` alone. Combining both status codes (as the
   reference power-model script this was cross-checked against does) would be
   a more robust passive-cooling detection than the current single-status read.

## Tests
`python3 tests/test_protocol.py` and `python3 tests/test_health.py` — both
standalone (stub HA, including a functional in-memory `Store` stub),
exit 0 == pass.

test_health.py (v0.3.2) covers: the closed-form F(2,d) quantile (converges
to χ²₂/2), the mode gate, day aggregation (thermal integration, Carnot
cross-check, gap capping), every qualification/rejection path, baseline →
T² → status incl. the ridge path and the self-masking percentile guard, a
45-day end-to-end simulation through 5-min samples with a 1 kWh-quantised
electrical counter plus an injected degradation, the YoY anchor, confidence
monotonicity, and the to_dict/from_dict round-trip. test_protocol.py gained
a health-tracker glue group (tick→sample→store→signal, disconnected no-op,
tracker restart round-trip).
Covers COP points (v0.3.1: anchor k-invariance, k=0 legacy equivalence,
blend continuity/monotonicity, defaults src=16.5/k=7, guard/clamp edges),
numeric decode, adversarial framing, watchdog, integrators incl. the
v0.3.1 re-sampling group (COP change mid-plateau, committing tick,
disconnect guard, no-arm-from-nothing), option parsing for all 7 fields
(incl. approach_k default/override/garbage/clamps) and `pumps_active`
transitions, and a full persistence round-trip (save → simulated restart →
load → signal replay → live-data overwrite → corrupt-store fallback).
