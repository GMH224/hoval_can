# Hoval CAN — Home Assistant Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
![Version](https://img.shields.io/badge/version-0.2.1-blue)
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

COP is **not** configurable — it is calculated automatically from live sensor data (see below).

---

## Entities

### Sensors (48)

#### Temperatures (7)
`outdoor_temp`, `room_temp`, `flow_temp`, `dhw_temp`, `heat_gen_temp`, `solar_storage_temp`\*, `circulation_temp`\*

\* Disabled by default.

#### Thermal Power & Performance (5)
`heat_pump_power` (%), `power_limit` (%), `compressor_modulation` (%), `current_heating_power` (kW), `wez_switch_cycles`

#### Dynamic COP (1) — new in v0.2.1
| Entity | Notes |
|---|---|
| `sensor.hoval_can_heat_pump_cop` | Live calculated COP; 0.0 when not running |

#### Electrical Power & Energy (6) — new in v0.2
| Entity | Unit | Persists |
|---|---|---|
| `sensor.hoval_can_heat_pump_electrical_power` | kW | — |
| `sensor.hoval_can_heat_pump_electrical_energy` | kWh | ✅ |
| `sensor.hoval_can_electric_heater_power` | kW | — |
| `sensor.hoval_can_electric_heater_energy` | kWh | ✅ |
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

### Two-regime formula

**Guard-rails:** if `m ≤ 1` or `t ≤ 12.5 °C` → COP = 0.0 (heat pump off / cold start)

**Space Heating regime** (t ≤ 40 °C) — low temperature lift:
```
cop_base = 0.5833 × m            if m < 12
         = 7.0                   if 12 ≤ m ≤ 22
         = 7.988 − 0.0449 × m   if m > 22

COP = cop_base × (17.5 / (t − 12.5))
```
Reference: lift = 17.5 °C → t_gen = 30 °C

**DHW regime** (t > 40 °C) — high temperature lift:
```
cop_base = 4.626 − 0.0417 × m   if m ≤ 33
         = 3.679 − 0.0130 × m   if 33 < m ≤ 60
         = 3.500 − 0.0100 × m   if m > 60

COP = cop_base × (39.5 / (t − 12.5))
```
Reference: lift = 39.5 °C → t_gen = 52 °C

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
          {% set t_source = 12.5 %}
          {% if m <= 1 or t <= t_source %}
            0.0
          {% else %}
            {% set lift = t - t_source %}
            {% if t <= 40 %}
              {% if m < 12 %}
                {% set cop_base = 0.5833 * m %}
              {% elif m <= 22 %}
                {% set cop_base = 7.0 %}
              {% else %}
                {% set cop_base = 7.988 - (0.0449 * m) %}
              {% endif %}
              {{ (cop_base * (17.5 / lift)) | max(1.0) | min(8.5) | round(4) }}
            {% else %}
              {% if m <= 33 %}
                {% set cop_base = 4.626 - (0.0417 * m) %}
              {% elif m <= 60 %}
                {% set cop_base = 3.679 - (0.0130 * m) %}
              {% else %}
                {% set cop_base = 3.500 - (0.0100 * m) %}
              {% endif %}
              {{ (cop_base * (39.5 / lift)) | max(1.0) | min(8.5) | round(4) }}
            {% endif %}
          {% endif %}
        availability: >
          {{ has_value('sensor.hoval_can_compressor_modulation') and
             has_value('sensor.hoval_can_heat_generator_temperature') }}
```

This should always match `sensor.hoval_can_heat_pump_cop` — it is the same formula using the same source entities.

### Physical basis

The formula models two distinct operating regimes with live temperature-lift correction:

- **cop_base** captures the heat pump's efficiency curve as a function of compressor loading
- **Lift ratio** (reference_lift / actual_lift) corrects for actual operating conditions — higher lift reduces COP, lower lift improves it, matching the second law of thermodynamics
- **Regime split at 40 °C** separates efficient floor-heating operation from high-temperature DHW mode

---

## Electrical Energy Calculation

### Heat pump
```
elec_power  = thermal_kW / COP(m, T_gen)   [0 when COP=0]
elec_energy += elec_power × elapsed_hours   (left Riemann sum, ~2 s intervals)
```
COP at the start of each interval is stored so accuracy is maintained when COP changes between updates.

### Electric heater
```
elec_power  = heater_rated_kW   (when heater active)
elec_energy += elec_power × elapsed_hours
```

### Total
Independent counter — not a runtime sum of sub-sensors.

### HA Energy Dashboard
Add individually under **Settings → Energy → Individual devices**:
- `sensor.hoval_can_heat_pump_electrical_energy`
- `sensor.hoval_can_electric_heater_energy`
- `sensor.hoval_can_total_electrical_energy`

---

## Electric Heater Detection

The Heizstab has no direct CAN-BUS datapoint. Detected as ON when:

1. DHW Status == 8 (DHW charging active)
2. DHW Temperature < DHW Setpoint
3. Heat Generator Temp ≤ DHW Temp + 5 °C (pump generator too cool to heat tank)

**Winter-safe:** heat pump at 40 °C for space heating cannot heat a 55 °C+ DHW tank; condition 3 fires correctly. Verified against a full 62 °C DHW capture.

---

## Changelog

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
