# CLAUDE.md — Hoval CAN Integration Developer Context

Version 0.3.0. Local-push HA integration for Hoval heat pumps via WLAN Gateway.
Read-only. TCP port 3113, proprietary CAN-BUS stream.

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
| 23009 | total_wez_electrical_energy | … | U32 dec=3 MWh | Hardware counter; persistent (entity-level, pre-v0.3.0) |

"Persistent (v0.3.0)" = in `PERSISTENT_DPIDS`: both entity-level restore
(`HovalPersistentSensor`) AND seeded into `coordinator._data` via the Store
before the sensor platform loads — see "Restart persistence" below.

Modbus register ≠ DatapointId (different address spaces). E.g. Modbus reg 19484 → DpId 9058.

---

## Dynamic COP (const.py :: calculate_cop)

### Formula
```python
def calculate_cop(modulation: float, heat_gen_temp: float,
                   source_temp: float = COP_SOURCE_TEMP) -> float:
    if modulation <= 1.0 or heat_gen_temp <= source_temp:
        return 0.0
    lift = heat_gen_temp - source_temp
    if heat_gen_temp <= 40.0:          # Space Heating
        if modulation < 12:   cop_base = 0.5833 * modulation
        elif modulation <= 22: cop_base = 7.0
        else:                  cop_base = 7.988 - 0.0449 * modulation
        cop = cop_base * (17.5 / lift)
    else:                              # DHW
        if modulation <= 33:   cop_base = 4.626 - 0.0417 * modulation
        elif modulation <= 60: cop_base = 3.679 - 0.0130 * modulation
        else:                  cop_base = 3.500 - 0.0100 * modulation
        cop = cop_base * (39.5 / lift)
    return max(1.0, min(8.5, round(cop, 4)))
```
`source_temp` defaults to `COP_SOURCE_TEMP` (12.5) but is normally called
from `coordinator.cop`, which passes `coordinator.source_temp_c` — the
configurable Options-flow value (v0.3.0; no CAN datapoint reports ground-loop
temperature on this installation).

### Constants to recalibrate (all in const.py)
```python
COP_SOURCE_TEMP  = 12.5  # fallback default only — normally overridden by the
                         # CONF_SOURCE_TEMP option (v0.3.0)
COP_SH_LIFT_REF  = 17.5  # space heating reference lift °C  (t_gen_ref = 30 °C)
COP_DHW_LIFT_REF = 39.5  # DHW reference lift °C           (t_gen_ref = 52 °C)
COP_SH_MAX_TGEN  = 40.0  # regime split temperature °C
COP_CLAMP_MIN    = 1.0
COP_CLAMP_MAX    = 8.5
```
Piecewise coefficients (0.5833, 7.0, 7.988, 0.0449, 4.626, 0.0417, 3.679, 0.0130,
3.500, 0.0100) are in the function body with comments — edit to recalibrate.

### Validation points (at reference lifts, default source_temp=12.5)
- Space heating t=30°C: m=12-22% → COP=7.0; m=33% → 6.51; m=50% → 5.74
- DHW t=52°C: m=33% → 3.25; m=60% → 2.90; m=100% → 2.50

### Entity subscriptions for COP-dependent sensors
| Sensor | Subscribes to |
|--------|--------------|
| HovalDynamicCOPSensor | dp_20052, dp_7 |
| HovalHeatPumpElecPowerSensor | dp_29051, dp_20052, dp_7 |
| HovalHeatPumpElecEnergySensor | dp_29051 (reads cop at each update) |
| HovalBrinePumpPowerSensor | dp_20052, cooling_signal |
| HovalHeatingPumpPowerSensor | dp_20052, cooling_signal |
| HovalStandbyPowerSensor | connection_signal only (constant value) |
| HovalTotalElecPowerSensor | dp_29051, dp_20052, dp_7, heater_signal, cooling_signal |
| HovalTotalElecEnergySensor | dp_29051, heater_signal, cooling_signal |

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

## Architecture

```
__init__.py      Setup; replays restored signals post-platform-setup;
                 options reload listener (preserves energy totals)
config_flow.py   ConfigFlow (IP+port); OptionsFlow (heater, cooling [legacy],
                 source_temp, brine_pump, heating_pump, standby — v0.3.0)
coordinator.py   TCP reader; frame parser; signals; Store-backed restart
                 persistence (v0.3.0); cop / heater_power_kw / source_temp_c /
                 brine_pump_kw / heating_pump_kw / standby_kw /
                 heat_pump_active / pumps_active properties
sensor.py        HovalSensor, HovalPersistentSensor, HovalDynamicCOPSensor,
                 HovalElectricHeaterPowerSensor, HovalElectricHeaterEnergySensor,
                 HovalPassiveCoolingPowerSensor/EnergySensor (legacy, v0.3.0),
                 HovalHeatPumpElecPowerSensor, HovalHeatPumpElecEnergySensor,
                 HovalBrinePumpPowerSensor, HovalHeatingPumpPowerSensor (v0.3.0),
                 HovalStandbyPowerSensor (v0.3.0),
                 HovalTotalElecPowerSensor, HovalTotalElecEnergySensor
binary_sensor.py HovalElectricHeaterBinarySensor
const.py         All constants; calculate_cop(source_temp param, v0.3.0);
                 sensor descriptions; PERSISTENT_DPIDS (extended v0.3.0);
                 STORAGE_VERSION / PERSIST_SAVE_DELAY_S (v0.3.0)
strings.json / translations/en.json   Options UI (6 fields as of v0.3.0)
```

## Dispatcher signals
- `hoval_can_{entry_id}_dp_{dp_id}` — DatapointId update
- `hoval_can_{entry_id}_electric_heater` — heater on/off change
- `hoval_can_{entry_id}_cooling` — passive-cooling on/off change (also used
  as the recompute trigger for `pumps_active`-dependent sensors, v0.3.0)
- `hoval_can_{entry_id}_connection` — TCP connected/disconnected

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
8. **status_heat_pump (DpId 2053)** is now decoded and persisted but not yet
   consumed by any derived property — `passive_cooling_on` still reads
   `status_heating_circuit` alone. Combining both status codes (as the
   reference power-model script this was cross-checked against does) would be
   a more robust passive-cooling detection than the current single-status read.

## Tests
`python3 tests/test_protocol.py` — standalone (stubs HA, including a
functional in-memory `Store` stub), exit 0 == pass.
Covers COP points (incl. custom source_temp), numeric decode, adversarial
framing, watchdog, integrators, the brine/heating-pump/standby option
parsing and `pumps_active` transition logic, and a full persistence
round-trip (save → simulated restart → load → signal replay → live-data
overwrite → corrupt-store fallback).
