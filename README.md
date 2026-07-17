# Hoval CAN — Home Assistant Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
![Version](https://img.shields.io/badge/version-0.3.0-blue)
![HA min version](https://img.shields.io/badge/HA-2023.1%2B-green)

Local-push integration for Hoval heat pump systems with the **WLAN Gateway**. Connects to the proprietary CAN-BUS TCP stream on port 3113. No cloud, no Modbus module required. Strictly **read-only** — nothing is ever written to the bus.

---

## Installation

### Via HACS
1. HACS → **Integrations** → ⋮ → **Custom repositories**
2. URL: `https://github.com/GMH224/hoval_can` — category **Integration**
3. Download, restart HA

### Manual
Copy `custom_components/hoval_can/` to your HA `custom_components/` directory, restart.

---

## Setup

**Settings → Devices & Services → Add Integration → Hoval CAN**

Enter the gateway IP address. Port defaults to 3113. HA tests the connection before saving.

### Options (Configure button)

**Settings → Devices & Services → Hoval CAN → Configure**

| Option | Default | Range | Notes |
|---|---|---|---|
| **Electric Heater Rated Power** | 3.0 kW | 0.5–12.0 kW | Check unit data plate |
| **Passive Cooling Power** | 100 W | 0–500 W | Legacy — kept for its own entity's history; **no longer** part of Total Electrical Power/Energy (see below) |
| **Ground-Loop Source Temperature** | 16.5 °C | −5–25 °C | No CAN datapoint reports this; adjust manually as the ground loop shifts seasonally (read the analog gauge on the line coming *from* the borehole during a long compressor run). Feeds the COP lift calculation. Default measured July 2026 during a DHW charge; ~15 °C expected in winter (200 m borehole, small annual swing) |
| **COP Approach Temperature k** | 7.0 °C | 0–15 °C | *(v0.3.1)* Combined evaporator + condenser heat-exchanger approach temperature in the COP lift correction. Preserves the calibrated anchors; prevents the COP from pinning at the 8.5 clamp at small lifts. **Calibration knob:** raise k if the integration over-reads electricity vs. the hardware counter (Total WEZ Electrical Energy, DpId 23009), lower it if it under-reads — each ±1 °C ≈ ∓2–3 % on space-heating electricity. 0 reproduces the pre-v0.3.1 formula |
| **Brine/Source Pump Power** | 30 W | 0–200 W | Ground-loop circulation pump. Same class of high-efficiency pump as the heating-circuit pump per Hoval's spec sheet — **estimate**, confirm against the actual pump if you can |
| **Heating Circuit Pump Power** | 20 W | 0–100 W | Median of the pump's own nameplate dynamic range (4–40 W) |
| **Standby Power** | 12 W | 0–100 W | TopTronic E controller + 3-way valve actuator idle draw. Always added, independent of heat pump/DHW/cooling state |

COP is **not** configurable — it is calculated automatically from live sensor data (see below); only the source temperature it needs is exposed here.

---

## Entities

### Sensors (56)

#### Temperatures (7)
`outdoor_temp`, `room_temp`, `flow_temp`, `dhw_temp`, `heat_gen_temp`, `solar_storage_temp`\*, `circulation_temp`\*

\* Disabled by default.

#### Thermal Power & Performance (5)
`heat_pump_power` (%), `power_limit` (%), `compressor_modulation` (%), `current_heating_power` (kW), `wez_switch_cycles`

#### Dynamic COP (1) — new in v0.2.1
| Entity | Notes |
|---|---|
| `sensor.hoval_can_heat_pump_cop` | Live calculated COP; 0.0 when not running |

#### Electrical Power & Energy (11) — extended in v0.3.0
| Entity | Unit | Persists |
|---|---|---|
| `sensor.hoval_can_heat_pump_electrical_power` | kW | — |
| `sensor.hoval_can_heat_pump_electrical_energy` | kWh | ✅ |
| `sensor.hoval_can_electric_heater_power` | kW | — |
| `sensor.hoval_can_electric_heater_energy` | kWh | ✅ |
| `sensor.hoval_can_passive_cooling_power` | kW | — (legacy — no longer part of Total, see below) |
| `sensor.hoval_can_passive_cooling_energy` | kWh | ✅ (legacy — no longer part of Total, see below) |
| `sensor.hoval_can_brine_pump_power` | kW | — |
| `sensor.hoval_can_heating_pump_power` | kW | — |
| `sensor.hoval_can_standby_power` | kW | — |
| `sensor.hoval_can_total_electrical_power` | kW | — |
| `sensor.hoval_can_total_electrical_energy` | kWh | ✅ |

#### Hardware Counter (1)
`sensor.hoval_can_total_wez_electrical_energy` (MWh, from device — persistent)

#### Status, Setpoints, Modes, Firmware extensions
See entity registry after installation.

### Binary Sensor
`binary_sensor.hoval_can_electric_heater_active` — True when Heizstab is running.

---

## Dynamic COP Calculation

COP is calculated automatically from two live sensor values — **no HA template helper needed**.

### Source entities
| Variable | Entity | DpId |
|---|---|---|
| `m` — modulation % | `sensor.hoval_can_compressor_modulation` | 20052 |
| `t` — heat generator °C | `sensor.hoval_can_heat_generator_temperature` | 7 |
| `t_source` — ground-loop temperature °C | *(no CAN datapoint — Options → Ground-Loop Source Temperature, default 16.5 °C as of v0.3.1)* | — |
| `k` — approach temperature °C | *(Options → COP Approach Temperature k, default 7.0 °C, v0.3.1)* | — |

### Two-regime formula

**Guard-rails:** if `m ≤ 1` or `t ≤ t_source` → COP = 0.0 (heat pump off / cold start)

Both regimes share the *effective lift* (v0.3.1):
```
lift_eff = (t − t_source) + k
```
`k` (default 7.0 °C, configurable) models the combined evaporator + condenser
heat-exchanger approach temperatures: the refrigerant works between roughly
`t_source − approach` and `t + approach`, so the effective lift saturates
instead of shrinking to zero. Because `k` is added to the reference lift too,
the model is **unchanged at its calibration anchors**, and `k = 0` reproduces
the pre-v0.3.1 bare-lift formula exactly.

**Space Heating regime** (t ≤ 38 °C) — low temperature lift:
```
cop_base = 0.5833 × m            if m < 12
         = 7.0                   if 12 ≤ m ≤ 22
         = 7.988 − 0.0449 × m   if m > 22

COP_SH = cop_base × ((17.5 + k) / lift_eff)
```
Reference: lift = 17.5 °C (t_gen = 30 °C at t_source = 12.5 °C)

**DHW regime** (t ≥ 42 °C) — high temperature lift:
```
cop_base = 4.626 − 0.0417 × m   if m ≤ 33
         = 3.679 − 0.0130 × m   if 33 < m ≤ 60
         = 3.500 − 0.0100 × m   if m > 60

COP_DHW = cop_base × ((39.5 + k) / lift_eff)
```
Reference: lift = 39.5 °C (t_gen = 52 °C at t_source = 12.5 °C)

**Blended transition** (38 °C < t < 42 °C, v0.3.1) — removes the ~16–19 %
power step the previous hard 40 °C split produced mid-DHW-charge:
```
w   = (t − 38) / 4
COP = (1 − w) × COP_SH + w × COP_DHW
```

Final result clamped to [1.0, 8.5].

### Verification template (optional cross-check)

To verify the integration's COP against raw HA values, add to `configuration.yaml`:

```yaml
template:
  - sensor:
      - name: "Hoval COP Verification"
        unique_id: hoval_cop_verification
        unit_of_measurement: "COP"
        state_class: measurement
        icon: mdi:heating-coil
        state: >
          {% set m = states('sensor.hoval_can_compressor_modulation') | float(0) %}
          {% set t = states('sensor.hoval_can_heat_generator_temperature') | float(0) %}
          {% set t_source = 16.5 %}
          {% set k = 7.0 %}
          {% if m <= 1 or t <= t_source %}
            0.0
          {% else %}
            {% set lift_eff = (t - t_source) + k %}
            {% if m < 12 %}{% set cb_sh = 0.5833 * m %}
            {% elif m <= 22 %}{% set cb_sh = 7.0 %}
            {% else %}{% set cb_sh = 7.988 - (0.0449 * m) %}{% endif %}
            {% if m <= 33 %}{% set cb_dhw = 4.626 - (0.0417 * m) %}
            {% elif m <= 60 %}{% set cb_dhw = 3.679 - (0.0130 * m) %}
            {% else %}{% set cb_dhw = 3.500 - (0.0100 * m) %}{% endif %}
            {% set cop_sh  = cb_sh  * ((17.5 + k) / lift_eff) %}
            {% set cop_dhw = cb_dhw * ((39.5 + k) / lift_eff) %}
            {% if t <= 38 %}{% set cop = cop_sh %}
            {% elif t >= 42 %}{% set cop = cop_dhw %}
            {% else %}
              {% set w = (t - 38) / 4 %}
              {% set cop = (1 - w) * cop_sh + w * cop_dhw %}
            {% endif %}
            {{ cop | round(4) | max(1.0) | min(8.5) }}
          {% endif %}
        availability: >
          {{ has_value('sensor.hoval_can_compressor_modulation') and
             has_value('sensor.hoval_can_heat_generator_temperature') }}
```

This should always match `sensor.hoval_can_heat_pump_cop` — it is the same formula using the same source entities. **Note:** the template above hardcodes `t_source = 16.5` and `k = 7.0` — if you've changed the Ground-Loop Source Temperature or COP Approach Temperature options away from their defaults, update these lines to match, or the verification will drift from the live sensor.

### Physical basis

The formula models two distinct operating regimes with live temperature-lift correction:

- **cop_base** captures the heat pump's efficiency curve as a function of compressor loading
- **Lift ratio** ((reference_lift + k) / (actual_lift + k)) corrects for actual operating conditions — higher lift reduces COP, lower lift improves it, matching the second law of thermodynamics. The **approach term k** (v0.3.1) reflects that the refrigerant cycle works across the heat-exchanger approach temperatures in addition to the water-side lift, so real machines *saturate* at small lifts rather than diverging — without it, a warm summer borehole (16.5 °C) plus floor-heating flow temperatures would pin the COP at the 8.5 clamp for most of the heating season, turning the model into a hardcoded constant exactly where it runs the most
- **Blended regime transition (38–42 °C, v0.3.1)** separates efficient floor-heating operation from high-temperature DHW mode without the step discontinuity a hard split produces in the electrical-power output mid-charge

---

## Electrical Energy Calculation

### Heat pump (compressor)
```
elec_power  = thermal_kW / COP(m, T_gen, source_temp, k)   [0 when COP=0]
elec_energy += elec_power × elapsed_hours   (left Riemann sum)
```
COP at the start of each interval is stored so accuracy is maintained when COP changes between updates. As of v0.3.1 the integrator re-samples on **every COP input** — thermal power (29051), modulation (20052) *and* T_gen (7) — and a 60-second timer additionally commits the open interval at freshly recomputed values. CAN broadcasts only on change, so previously a long constant-thermal plateau (a DHW charge holding max power while T_gen climbs and COP falls) was integrated at the COP frozen at the start of the plateau; staleness is now capped at 60 s. The same applies to Total Electrical Energy, whose 60-second tick now *commits* at a recomputed rate instead of only refreshing the display. `source_temp` (default 16.5 °C) and `k` (default 7.0 °C) are the configurable options above — no CAN datapoint reports them.

### Electric heater
```
elec_power  = heater_rated_kW   (when heater active)
elec_energy += elec_power × elapsed_hours
```

### Brine pump / Heating pump (new in v0.3.0)
```
pumps_active = heat_pump_active (modulation > threshold) OR passive_cooling_on
elec_power    = brine_pump_kw + heating_pump_kw   (when pumps_active)
elec_energy  += elec_power × elapsed_hours
```
Both pumps share the same trigger: they run whenever the compressor is drawing (heating/DHW) *or* the heating circuit is in passive/free cooling, since passive cooling circulates the ground loop through the floor circuit with the compressor bypassed.

### Standby (new in v0.3.0)
```
elec_power = standby_kw   (always, whenever the gateway is connected)
```
Not zero-filled like the other terms — this is the one component that's always a real number, so Total Electrical Power never sits at an artificial 0 during idle periods.

### Total
Independent counter — not a runtime sum of sub-sensors. As of v0.3.0:
```
total = heat_pump_elec + heater_elec + brine_pump_elec + heating_pump_elec + standby_elec
```
Passive Cooling Power/Energy (the pre-v0.3.0 lump estimate) is **no longer** part of this sum — it modelled the same physical pumps that Brine Pump Power + Heating Pump Power now cover individually; keeping both would double-count. The Passive Cooling entities themselves are unchanged and still update, purely so their own history isn't lost.

### HA Energy Dashboard
Add individually under **Settings → Energy → Individual devices**:
- `sensor.hoval_can_heat_pump_electrical_energy`
- `sensor.hoval_can_electric_heater_energy`
- `sensor.hoval_can_total_electrical_energy`

`sensor.hoval_can_total_electrical_energy` keeps the same `unique_id`/`entity_id` across the v0.3.0 upgrade — no dashboard changes needed, the counter just accrues a bit faster going forward (extra standby + pump load that wasn't tracked before).

---

## Electric Heater Detection

The Heizstab has no direct CAN-BUS datapoint. Detected as ON when:

1. DHW Status == 8 (DHW charging active)
2. DHW Temperature < DHW Setpoint
3. Heat Generator Temp ≤ DHW Temp + 5 °C (pump generator too cool to heat tank)
4. Compressor Modulation ≤ 1 % (compressor not running)

**DHW priority:** a single compressor cannot heat the house and the DHW tank at
once (3-way diverter / DHW takes priority), so while the tank is charging a
running compressor is itself doing the heating and the Heizstab is off — it only
finishes the charge once the heat pump stops. Condition 4 removes false ON
pulses during the compressor's DHW-charge ramp, when the generator temperature
lags and condition 3 alone would briefly read true. Verified against a full
62 °C DHW capture.

---

## Auxiliary Loads (Brine Pump, Heating Pump, Standby) — new in v0.3.0

Beyond the compressor and the DHW heater, three more always-relevant loads are now modelled, each configurable in Options:

| Load | Entity | Trigger | Default |
|---|---|---|---|
| Ground-loop (brine/source) pump | `sensor.hoval_can_brine_pump_power` | Compressor active **or** passive cooling active | 30 W |
| Heating-circuit pump | `sensor.hoval_can_heating_pump_power` | Same trigger as brine pump | 20 W |
| Standby (controller + valve actuator) | `sensor.hoval_can_standby_power` | Always, whenever connected | 12 W |

The brine and heating pump share a trigger because passive ("free") cooling circulates the ground loop through the floor circuit with the compressor bypassed — both pumps physically run in that mode, not just during active heating/DHW. This is why the older single `cooling_power_kw` estimate was retired from the Total calculation: it modelled the same two pumps as one lump figure, and keeping it alongside the new per-pump terms would double-count.

The brine pump's default (30 W) is an estimate — Hoval's own spec sheet confirms it's the same class of speed-regulated high-efficiency circulator as the heating-circuit pump (not a separate high-draw unit), but the exact wattage hasn't been independently measured on this installation. Update the option once you've confirmed it against the actual pump.

---

## Restart Persistence — new in v0.3.0

CAN only re-broadcasts a datapoint when its value changes, which is a problem for Total Electrical Power specifically: if HA restarts while, say, the Heat Pump Status hasn't changed in hours, that datapoint might not arrive again for a long time — leaving the derived power sensors sitting at Unknown in the interim, which breaks the Energy dashboard's long-term statistics for that gap.

To close this, the coordinator now persists the last-known value of the datapoints that feed `cop`, `electric_heater_on`, `passive_cooling_on`, and `heat_pump_active`/`pumps_active` (`status_heat_pump`, `status_heating_circuit`, `status_dhw`, `compressor_modulation`, `heat_gen_temp`, `current_heating_power`, `dhw_temp`, `dhw_setpoint`) using Home Assistant's `Store` helper, debounced to one write per 30 s. On restart, this is loaded *before* the sensor platform is even set up, and replayed as dispatcher signals once entities are subscribed — so Total Electrical Power resolves to a real number immediately after a restart instead of waiting for the next CAN broadcast.

Live CAN data always overwrites a restored value the moment it arrives. A corrupt or missing store degrades gracefully to a cold start (today's pre-v0.3.0 behaviour) rather than blocking the integration from loading.

---

## Changelog

### v0.3.1 — COP model refinement (approach-k, regime blend), integrator re-sampling
- **New option: COP Approach Temperature k** (default 7.0 °C, range 0–15 °C).
  The lift correction becomes `(ref_lift + k) / (lift + k)` — preserving the
  calibrated anchor points exactly while saturating the curve at small lifts
  instead of diverging into the 8.5 clamp. `k = 0` reproduces the previous
  formula bit-for-bit. This is the calibration knob against the hardware
  counter (Total WEZ Electrical Energy, DpId 23009): raise k if the
  integration over-reads electricity, lower if it under-reads (±1 °C ≈ ∓2–3 %
  on space-heating electricity).
- **Ground-Loop Source Temperature default 12.5 → 16.5 °C** — measured on the
  analog Erdsonde gauge during an active DHW charge (July 2026; 200 m borehole
  with summer passive-cooling recharge, ~15 °C expected in winter). A value
  you already saved in Options is **not** changed — the new default only
  applies to fresh installs or if you reset the field.
- **Blended regime transition (38–42 °C)** replaces the hard 40 °C split —
  removes the ~16–19 % step in Heat Pump Electrical Power mid-DHW-charge.
- **Energy integrators re-sample all COP inputs**: Heat Pump Electrical
  Energy and Total Electrical Energy now subscribe to modulation (20052) and
  T_gen (7) in addition to thermal power (29051), and their 60-second timer
  *commits* the open interval at freshly recomputed values (previously the
  Total's timer only refreshed the display and the HP integrator had no timer
  at all). Since CAN broadcasts only on change, this caps COP staleness in
  the integral at one minute — previously a constant-power DHW plateau was
  integrated end-to-end at the COP frozen at its start. The timers are
  guarded on the gateway connection, so downtime is still never integrated
  and startup/restore semantics are unchanged.
- **Expected effect vs. v0.3.0 at the defaults:** computed electrical
  consumption drops ≈ 7–9 % on DHW and ≈ 12–19 % on space heating (0 % where
  both formulas clamp) — the previous defaults systematically over-estimated.
  Counters are TOTAL_INCREASING lifetime values: history is not rewritten,
  only the accumulation rate changes from the upgrade onward. Note the
  changeover date when comparing Energy-dashboard periods across it.
- `unique_id`/`entity_id` of all entities unchanged — no dashboard edits.
- Tests: COP suite rewritten (anchor k-invariance, k=0 legacy equivalence,
  blend continuity/monotonicity, new defaults, clamp/guard edges); new
  integrator re-sampling group (COP change mid-plateau, tick commit,
  disconnect guard, no-arm-from-nothing); options tests extended for k.

### v0.3.0 — Auxiliary loads, configurable source temp, restart persistence
- **New options:** Ground-Loop Source Temperature (12.5 °C default, replaces the
  hardcoded COP source-temperature constant), Brine/Source Pump Power (30 W),
  Heating Circuit Pump Power (20 W), Standby Power (12 W).
- **New entities:** Brine Pump Power, Heating Pump Power, Standby Power (all
  kW). Brine/heating pump power is active whenever the compressor is drawing
  *or* the heating circuit is in passive/free cooling.
- **Total Electrical Power/Energy** now includes the two pumps and standby;
  the older `cooling_power_w` lump estimate is retired from the Total (it
  modelled the same pumps as one figure — see "Auxiliary Loads" above) but its
  own entity is unchanged, so its history isn't lost. Standby is never
  zero-filled, so the Total never reads an artificial 0 while the gateway is
  connected. `unique_id`/`entity_id` for both Total entities are **unchanged**
  — no dashboard changes needed.
- **New: coordinator-level restart persistence** (see "Restart Persistence"
  above) — closes a real gap where CAN's on-change-only broadcasting could
  leave Total Electrical Power at Unknown for a long time after a restart.
- Tests extended: new option defaults/overrides/clamping, `calculate_cop` with
  a custom source temperature, `heat_pump_active`/`pumps_active` transition
  logic, and a full save → restart → load → signal-replay → live-overwrite
  persistence round-trip against a functional in-memory `Store` stub.

### v0.2.8 — Total electrical zero-fills unknown inputs
- **Total Electrical Power/Energy now zero-fill unknown inputs** instead of
  reading *unknown* whenever any single input is absent. On CAN some datapoints
  (e.g. `status_dhw`) stay dormant until the heat pump engages, which previously
  blanked the total — and dropped passive-cooling energy — through long
  cooling-only spells. A genuinely dead/stalled link is still surfaced via the
  entity `available` state (connection + data watchdog), so this only affects
  not-yet-seen datapoints, where 0 is the honest contribution. The standalone
  *Heat Pump Electrical Power* sensor is unchanged (single-term, stays unknown
  until thermal power is first seen).

### v0.2.7 — Heizstab DHW-priority fix
- **Fixed false electric-heater (Heizstab) ON pulses** during heat-pump DHW
  charging. The detection now also requires the compressor to be off
  (modulation ≤ 1 %): under DHW priority a running compressor is itself heating
  the tank, so the Heizstab is reported off until the heat pump stops and
  finishes the charge electrically. This removes the spurious 4 kW spikes,
  including the compressor start-up window where the generator temperature lags
  below the tank and the previous temperature-only test briefly read true.
- The fix flows through automatically to **Electric Heater Power/Energy** and to
  **Total Electrical Power/Energy**, which all derive from the same
  `electric_heater_on` signal. (Energy counters correct going forward only;
  reset their stored state if you want a clean baseline.)
- Heater state now also recomputes on **Compressor Modulation** (DpId 20052)
  updates, so the off transition propagates immediately.
- Docs (README, CLAUDE.md) updated to document condition 4.

### v0.2.6 — Windowed health rates
- **New rate sensors** (diagnostic), giving an at-a-glance health read instead
  of raw cumulative counters: **Gateway Throughput** (decoded datapoints/min,
  sliding 60-min window) and **Gateway Framing Error Rate** (errors/hour,
  sliding 15-min window). Together with **Gateway Data Age** these form a
  RED-style triad — Rate (throughput), Errors (error rate), Duration
  (freshness) — that cleanly disambiguates "stream dirty" from "link down".
- Rates are computed from a bounded ring of 60-second snapshots (≈60 samples,
  hard-capped); restart-robust; report *unknown* during a short warm-up. The
  leading edge uses the live counters, so a stalled stream decays the rate to 0
  rather than freezing it. The cumulative counters are retained for exporters
  and for HA's own Derivative/Statistics helpers.
- A throughput-normalised *errors-per-decoded* ratio was deliberately **not**
  shipped: its denominator collapses exactly when the stream stalls (the worst
  moment), and for this near-constant-cadence device the windowed error *rate*
  conveys the same health without that instability.
- Rates added to the downloadable diagnostics; tests extended (pure rate
  function edge cases, property wiring with a deterministic clock, prune logic,
  sensor passthrough). Suite total: 86 assertions, all pass.

### v0.2.5 — Diagnostics / telemetry pack
- **New diagnostic sensor entities** (category *diagnostic*), promoting the
  health counters that were previously only attributes into first-class,
  recordable/alarmable states: **Gateway Data Age** (s), **Gateway Reconnects**,
  **Gateway Framing Errors**, and **Gateway Datapoints Decoded** (derive a
  rate for throughput). These are recorded to long-term statistics and are
  visible to state-based exporters (InfluxDB / Prometheus / MQTT); they stay
  available while disconnected so staleness is observable.
- **Downloadable config-entry diagnostics** (`diagnostics.py`): the
  Settings → Devices & Services → "Download diagnostics" button now returns a
  redacted JSON snapshot of connection health, options, derived states, and the
  last-seen value of every decoded datapoint — for incident triage without
  shell access. Host/IP and unique_id are redacted.
- Tests extended for the snapshot structure, host redaction, missing-coordinator
  handling, and each diagnostic sensor's value wiring.

### v0.2.4 — Passive cooling energy tracking
- **New: passive ("free") cooling power & energy.** When the Heating Circuit
  Status (DpId 2051) reports passive cooling (value 9), the circulation-pump
  draw is now tracked. Two new entities are added — **Passive Cooling Power**
  (kW) and **Passive Cooling Energy** (kWh, cumulative) — and the term is also
  folded into **Total Electrical Power** and **Total Electrical Energy**.
- **New option: Passive Cooling Power (W)** — configurable in the integration
  options, range 0–500 W, default 100 W (set to your circulation-pump draw).
  Stored in watts; converted to kW internally.
- The cooling term is purely additive: passive cooling runs with the compressor
  off, so it does not overlap the COP-based heat-pump term (which is 0 when the
  compressor is idle). Installations without a cooling circuit are unaffected —
  unknown cooling status is treated as 0 W, so the electrical totals never
  regress.
- Tests extended (`tests/test_protocol.py`) covering the new config property,
  status detection, edge-triggered dispatch, the combined-power formula, the
  no-regression rule, and the energy integration.

### v0.2.3 — Frame-integrity pass
- **Fix: a value byte-pair equal to the frame markers could mis-frame data.**
  The receiver previously split the stream only on the start marker (`FF 01`),
  so a datapoint value containing `FF 01` (or, after the v0.2.2 cleanup, ending
  in `FF 02`) could corrupt that frame and occasionally the next one. The
  receiver is now a **length-aware parser**: for every monitored fixed-width
  datapoint the value length comes from the type table and the `FF 02`
  end-marker position is verified, so in-value `FF 01`/`FF 02` can no longer
  corrupt a monitored sensor. Variable-length (STR) and unmapped frames fall
  back to end-marker scanning; a frame whose end-marker is misplaced is counted
  as a desync and the parser resyncs to the next start marker — a monitored
  sensor is never updated from a mis-delimited frame (at worst a sample is
  dropped).
- **Observability:** the diagnostic connectivity sensor now also exposes
  `framing_errors` (cumulative desync count) so frame health is alarmable.
- **Tests:** added `tests/test_protocol.py` — a standalone suite (no Home
  Assistant install required) covering the COP model, numeric decoder,
  adversarial framing (in-value markers, split reads, desync recovery, LE/STR),
  the connection watchdog, and integrator arithmetic.

### v0.2.2 — Reliability / industrial-hardening pass
- **Fix: integration silently stopped recording data until manual reload.** The
  TCP read loop treated a half-open connection (gateway reboot / Wi-Fi drop with
  no FIN/RST) as normal silence and looped forever, leaving every sensor frozen
  while still reporting "available". Added a **byte-level inactivity watchdog**
  (reconnect after 90 s with no bytes) plus a **data-level watchdog** (reconnect
  after 300 s with no *decodable* datapoint, catching a live-but-desynced
  stream), and enabled tuned **TCP keep-alive** on the socket.
- **Fix: false energy spike after a reconnect.** Energy integrators now discard
  the open interval on disconnect, so the first sample after recovery no longer
  integrates the entire downtime as one lump of kWh.
- **Fix: energy totals corrupted by wall-clock steps.** All energy integration
  now uses a **monotonic clock** instead of `datetime.now()`; NTP/DST steps can
  no longer lose energy (backward step) or over-count (forward step).
- **Hardening: bounded RX buffer.** The receive buffer is now capped (64 KiB)
  and resynced on overflow, removing an unbounded-growth / memory-exhaustion
  path when the frame separator never appears.
- **Hardening: reconnect storm control.** Reconnects use capped exponential
  backoff with jitter (10 → 120 s), and repeated failure logs are de-duplicated
  (first at WARNING, repeats at DEBUG, recovery announced) to prevent log flood.
- **Lifecycle: tracked background task.** The reader task is now owned by the
  config entry, so it is reliably cancelled on unload/shutdown (no orphaned
  task warnings).
- **Observability: new `binary_sensor.hoval_can_gateway_connection`**
  (diagnostic, device-class connectivity). Reports OFF on disconnect and exposes
  `last_data_age_seconds`, `reconnect_count`, and `last_error` — suitable as an
  ICS staleness/health alarm source.

### v0.2.1
- **Dynamic COP** replaces fixed configurable COP. Calculated from `compressor_modulation` and `heat_generator_temperature` using a two-regime piecewise model with live temperature-lift correction
- New sensor: `sensor.hoval_can_heat_pump_cop`
- **Electric Heater Rated Power** now configurable via Configure menu (0.5–12.0 kW, default 3.0 kW)
- COP removed from options — it is now fully automatic

### v0.2.0
- Heat Pump Electrical Power/Energy, Total Electrical Power/Energy
- Options flow (configurable COP — superseded in v0.2.1)
- All energy sensors persistent

### v0.1.x
- Initial release, persistent energy fixes

---

## License
MIT
