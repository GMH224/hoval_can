# CLAUDE.md — Hoval CAN Integration Developer Context

Version 0.2.7. Local-push HA integration for Hoval heat pumps via WLAN Gateway.
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
| 7 | heat_gen_temp | `sensor.hoval_can_heat_generator_temperature` | S16 dec=1 °C | **COP input** |
| 20052 | compressor_modulation | `sensor.hoval_can_compressor_modulation` | U8 % | **COP input** |
| 29051 | current_heating_power | `sensor.hoval_can_current_heating_power` | U32 dec=1 kW | Energy integration |
| 2052 | status_dhw | `sensor.hoval_can_status_dhw` | U8 | 8=charging → heater detection |
| 4 | dhw_temp | `sensor.hoval_can_dhw_temp` | S16 dec=1 °C | Heater detection |
| 1004 | dhw_setpoint | `sensor.hoval_can_dhw_setpoint` | S16 dec=1 °C | Heater detection |
| 23009 | total_wez_electrical_energy | … | U32 dec=3 MWh | Hardware counter |

Modbus register ≠ DatapointId (different address spaces). E.g. Modbus reg 19484 → DpId 9058.

---

## Dynamic COP (const.py :: calculate_cop)

### Formula
```python
def calculate_cop(modulation: float, heat_gen_temp: float) -> float:
    t_source = 12.5   # COP_SOURCE_TEMP
    if modulation <= 1.0 or heat_gen_temp <= t_source:
        return 0.0
    lift = heat_gen_temp - t_source
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

### Constants to recalibrate (all in const.py)
```python
COP_SOURCE_TEMP  = 12.5  # heat source temperature °C
COP_SH_LIFT_REF  = 17.5  # space heating reference lift °C  (t_gen_ref = 30 °C)
COP_DHW_LIFT_REF = 39.5  # DHW reference lift °C           (t_gen_ref = 52 °C)
COP_SH_MAX_TGEN  = 40.0  # regime split temperature °C
COP_CLAMP_MIN    = 1.0
COP_CLAMP_MAX    = 8.5
```
Piecewise coefficients (0.5833, 7.0, 7.988, 0.0449, 4.626, 0.0417, 3.679, 0.0130,
3.500, 0.0100) are in the function body with comments — edit to recalibrate.

### Validation points (at reference lifts)
- Space heating t=30°C: m=12-22% → COP=7.0; m=33% → 6.51; m=50% → 5.74
- DHW t=52°C: m=33% → 3.25; m=60% → 2.90; m=100% → 2.50

### Entity subscriptions for COP-dependent sensors
| Sensor | Subscribes to |
|--------|--------------|
| HovalDynamicCOPSensor | dp_20052, dp_7 |
| HovalHeatPumpElecPowerSensor | dp_29051, dp_20052, dp_7 |
| HovalHeatPumpElecEnergySensor | dp_29051 (reads cop at each update) |
| HovalTotalElecPowerSensor | dp_29051, dp_20052, dp_7, heater_signal |
| HovalTotalElecEnergySensor | dp_29051, heater_signal |

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

## Architecture

```
__init__.py      Setup; options reload listener (preserves energy totals)
config_flow.py   ConfigFlow (IP+port); OptionsFlow (heater_power_kw only)
coordinator.py   TCP reader; frame parser; signals; cop + heater_power_kw properties
sensor.py        HovalSensor, HovalPersistentSensor, HovalDynamicCOPSensor,
                 HovalElectricHeaterPowerSensor, HovalElectricHeaterEnergySensor,
                 HovalHeatPumpElecPowerSensor, HovalHeatPumpElecEnergySensor,
                 HovalTotalElecPowerSensor, HovalTotalElecEnergySensor
binary_sensor.py HovalElectricHeaterBinarySensor
const.py         All constants; calculate_cop(); sensor descriptions
strings.json / translations/en.json   Options UI (heater power only)
```

## Dispatcher signals
- `hoval_can_{entry_id}_dp_{dp_id}` — DatapointId update
- `hoval_can_{entry_id}_electric_heater` — heater on/off change
- `hoval_can_{entry_id}_connection` — TCP connected/disconnected

---

## Known gaps / future work
1. **0x70 PUSH-MULTI** — compact status blocks not decoded (skipped by the
   length-aware parser via end-marker scanning; never corrupts monitored dpids)
2. **LIST text labels** — DpIds 3050/9075 show integer codes
3. **COP_SOURCE_TEMP** not yet in options flow (recalibrate in const.py)
4. **Winter heater capture** — detection logic correct but not validated live
5. **Write support** — 0x56 frames observed; format partially characterised
6. **Frame value-field offset** — the parser assumes the value sits at byte 10
   immediately before `FF 02` (per the documented layout). If the device ever
   emits trailing bytes before `FF 02`, the `framing_errors` counter rises and
   the data watchdog forces reconnects — i.e. the assumption is observable and
   fail-safe, not silent. Re-confirm against a live capture if `framing_errors`
   is non-trivial in the field.

## Tests
`python3 tests/test_protocol.py` — standalone (stubs HA), exit 0 == pass.
Covers COP points, numeric decode, adversarial framing, watchdog, integrators.
