# CLAUDE.md — Hoval CAN Integration: Developer Context

This file documents the reverse-engineering work, protocol findings, and
architectural decisions for future development (including AI-assisted).

---

## Overview

Local-push Home Assistant integration for Hoval heat pumps with the WLAN
Gateway. Connects to TCP port 3113 (proprietary CAN-BUS stream). Strictly
read-only. Version 0.2.0.

---

## Hardware

- **Device**: Hoval TopTronic E heat pump (WEZ) with floor heating (HKW) and DHW
- **Gateway**: Hoval WLAN Gateway module
- **Interface**: TCP port 3113 — proprietary binary CAN-BUS stream
- **Not available**: Modbus TCP (port 502) requires a separate hardware module not present

---

## Protocol (reverse-engineered from binary captures, May–June 2026)

### Frame structure

```
[FF 01]         ← frame separator (between frames in the TCP stream)
[byte 0–2]      3-byte routing header  (usually 00 00 00)
[byte 3–4]      2-byte unit ID, big-endian  (e.g. 0x0201 = 513 = WEZ unit 1)
[byte 5]        1-byte command code
[byte 6–7]      2-byte group ID, big-endian
[byte 8–9]      2-byte DatapointId, big-endian
[byte 10+]      value bytes (length determined by TypeName in the datapoint list)
[FF 02]         ← end-of-frame marker
```

### Command codes

| Code | Name | Action |
|---|---|---|
| 0x40 | READ-REQUEST | Master polls device — ignore |
| **0x42** | **READ-RESPONSE BE** | **Big-endian value — decode** |
| 0x44 | REQ-VARIANT | Alternate request — ignore |
| 0x56 | WRITE / BOUNDS | Write command or min/max bounds — ignore (read-only) |
| 0x61 | ERROR/NAK | Ignore |
| **0x62** | **READ-RESPONSE LE** | **Little-endian from room display — decode** |
| 0x70 | PUSH-MULTI | Compact status block — not decoded in v0.2 |
| 0x74 | PUSH-NULL | Null status block — ignore |

### 0x42 decoding (standard big-endian)

```python
value_bytes = frame[10:]
raw_int = struct.unpack('>' + fmt, value_bytes[:nbytes])[0]
decoded = raw_int / 10**decimal
```

### 0x62 decoding (little-endian, from room display unit 0x2201)

```python
# frame[10] = status byte (0x00 = OK)
# frame[11:13] = 2-byte little-endian signed value
value_bytes = frame[11:13]
raw_int = struct.unpack('<h', value_bytes)[0]
decoded = raw_int / 10**decimal
```

0x62 only carries data when `group_id & 0x8000` (high bit set = response).
Low-bit group = request frame, no data.

### TypeName → byte count

| TypeName | Bytes | struct fmt |
|---|---|---|
| U8 / S8 | 1 | B / b |
| U16 / S16 / LIST | 2 | H / h / H |
| U32 / S32 | 4 | I / i |
| S64 | 8 | q |

### Null sentinels (value = "not connected" / sensor error)

| Type | Null value |
|---|---|
| S8 | −128 |
| U16 | 0x8000 (bleeds from S16 convention on some sensors, e.g. DpId=118) |
| S16 | −32768 (0x8000) |
| U32 | 0x80000000 or 0xFFFFFFFF |
| S32 | −2147483648 |
| Any | All bytes = 0xFF |

### Groups to skip

Groups 15614, 15922, 15923, 15924 carry time-schedule data.
DatapointIds 64000–64050 carry single-byte time slot values (N × 10 min from midnight).
Neither is decoded as HA sensor entities.

---

## DatapointId reference

The DatapointId in the CAN-BUS stream corresponds to the "DatapointId" column
in `Hoval_Modbus_Datenpunktliste_Stand_April_2026.xlsx`.

**Important**: The "Register Address" column in that Excel is the MODBUS register
number — a completely different address space. Do not confuse them.
E.g. Modbus register 19484 → DatapointId 9058 — they are unrelated numbers.

### Ambiguous DatapointIds

Some DatapointIds appear under multiple unit types with different TypeNames.
The loader prefers the entry with the smallest UnitId (WEZ UnitIds 1–8 beat
FW UnitIds 193+) because captured data matched WEZ TypeNames.

### Firmware-extension DatapointIds (not in April 2026 Excel)

| DpId | Observed content | Best guess |
|---|---|---|
| 500 | U8 = 4 | Heating program count or index |
| 502, 505 | ASCII string "Summer" | Active heating program name |
| 503, 504 | U8 = 0/1 | Program flags |
| 2010, 2011, 2018 | S16 = 0 | Unknown status/counter |
| 3054 | S16, °C | Normal room temp for 2nd heating circuit |
| 3058, 3078 | S16 | Unknown HC setpoints |
| 4005 | ASCII string "Bodenheizung" | Heating circuit name |
| 7014 | U8 = 3 | WEZ status flag |
| 20125 | U8 = 0/1 | WEZ operational flag |

---

## Electric heater detection

### Why there is no direct datapoint

DpId=1029 (Betriebsst. Zusatzhzg., operating hours) and DpIds 482/483
(web-sourced Status/Leistung Zusatzheizung, unverified) were searched across
18,000+ frames including a full 62 °C DHW heating cycle (June 2026 captures).
Neither appeared. The electric heater does not broadcast its state via the
WLAN Gateway's port 3113 stream.

### Detection algorithm — `coordinator.py :: electric_heater_on`

```python
heater_on = (
    status_ww == 8              # DHW demand active (DpId 2052)
    and dhw_actual < dhw_sp     # target not reached (DpId 4 < DpId 1004)
    and heat_gen <= dhw_actual + 5.0  # generator not hot enough (DpId 7)
)
```

Returns `None` if any of the four source DpIds has not yet been received.

### Why condition 3 is winter-safe

The heat pump can only heat the DHW tank if its generator temperature is
ABOVE the tank temperature. When `heat_gen ≤ dhw_actual + 5 °C`, the heat
pump cannot contribute heat to the tank regardless of what it is doing for
space heating. Only the electric element can raise the DHW temperature.

Summer verified: heat pump ran DHW to 52 °C (generator 58 °C), then stopped.
DHW continued +2.5 °C in 17 min → 2.9 kW ≈ 3 kW electric element confirmed.
Winter logic is theoretically correct but not yet validated against a capture.

### Rated power

3.0 kW, confirmed empirically: `280 L × 4186 J/kg·K × 2.5 °C ÷ (17×60 s) ≈ 2.9 kW`
Change `HEATER_RATED_POWER_KW` in `const.py` if unit has a different element.

---

## Electrical energy sensors (v0.2)

### COP

Stored in `entry.options[CONF_COP]`, default 6.3. Read via `coordinator.cop`
property. When the user changes COP via the options flow, `__init__.py`
triggers `async_reload_entry` which recreates everything. RestoreEntity
preserves all accumulated totals across this reload.

### HovalHeatPumpElecEnergySensor

Integrates `thermal_kW / COP × elapsed_hours` using a left Riemann sum.
Updates on every DpId=29051 dispatch signal (~2 s intervals from captures).
If DpId=29051 returns None (connection lost or null sentinel), `_last_ts`
is reset to None so the gap is not accumulated on reconnect.

### HovalTotalElecEnergySensor

Integrates `(thermal_kW / COP + heater_kW) × elapsed_hours`.
Subscribes to both DpId=29051 signal and heater_signal.
Independent persistent counter — not a sum of sub-sensors at runtime.
A 60-second interval timer keeps the displayed value current between
heater state transitions (which are infrequent).

### Integration precision

With DpId=29051 updating every ~2 s, the left Riemann sum has negligible
truncation error for typical heat pump cycles (30+ min). The only
significant error source is the fixed COP assumption.

---

## Architecture

```
__init__.py          Setup/teardown; registers options reload listener
config_flow.py       ConfigFlow (IP + port); OptionsFlow (COP)
coordinator.py       TCP reader; frame parser; data store; dispatcher signals
                     Exposes: get_value(), electric_heater_on, cop, connected
sensor.py            8 entity classes:
                       HovalSensor                 — standard dpId
                       HovalPersistentSensor       — dpId with RestoreEntity
                       HovalElectricHeaterPowerSensor
                       HovalElectricHeaterEnergySensor (RestoreEntity)
                       HovalHeatPumpElecPowerSensor
                       HovalHeatPumpElecEnergySensor   (RestoreEntity)
                       HovalTotalElecPowerSensor
                       HovalTotalElecEnergySensor      (RestoreEntity)
binary_sensor.py     HovalElectricHeaterBinarySensor
const.py             All constants: sensor descriptions, protocol, COP config
strings.json         Config + options flow UI strings
translations/en.json Same (English)
```

### Dispatcher signals

| Signal | Fired when |
|---|---|
| `hoval_can_{entry_id}_dp_{dp_id}` | A specific DatapointId updates |
| `hoval_can_{entry_id}_electric_heater` | Electric heater on/off changes |
| `hoval_can_{entry_id}_connection` | TCP connection established or lost |

---

## Standalone reader script

`hoval_raw_reader.py` (v4) was used during reverse engineering.

```bash
# Live capture for 75 minutes
python hoval_raw_reader.py --ip 192.168.x.x \
    --datapoints "Hoval_Modbus_Datenpunktliste_Stand_April_2026.xlsx" \
    --duration 75 --outdir ./capture

# Replay a capture
python hoval_raw_reader.py --replay hoval_raw_YYYYMMDD_HHMMSS.bin \
    --datapoints "Hoval_Modbus_Datenpunktliste_Stand_April_2026.xlsx" \
    --stats-interval 0 --no-log

# Look up a Modbus register address → DatapointId
python hoval_raw_reader.py \
    --datapoints "Hoval_Modbus_Datenpunktliste_Stand_April_2026.xlsx" \
    --lookup-reg 19484
```

---

## Binary captures used during development

| File | Frames | Context |
|---|---|---|
| hoval_raw_20260531_074801.bin | 503 | Morning, summer, heat pump idle |
| hoval_raw_20260531_183046.bin | ~2000 | Evening, DHW legionella cycle |
| hoval_decoded_20260531_233008.log | 5000 | Night, large capture |
| hoval_raw_20260603_090529.bin | 14594 | 62 °C DHW cycle — heat pump phase |
| hoval_raw_20260603_095039.bin | 4319 | 62 °C DHW cycle — electric heater phase |

---

## Known gaps / future work

1. **0x70 PUSH-MULTI** — ~80 frames per 500 with compact status blocks
   (grp 0xA100, 0xA300, 0xA600). Structure: `[count_byte][count × U8]`.
   Likely per-circuit status arrays. Not decoded in v0.2.

2. **LIST type text labels** — DpIds 3050, 9075 return integer codes.
   Text values (e.g. "Standby", "Woche 1") are in the Excel Text 0..N columns
   but not loaded. A future version could expose these as readable mode names.

3. **Dynamic COP curve** — replace the fixed COP with a lookup table mapping
   outdoor temperature → COP for more accurate instantaneous power estimation.

4. **Winter heater capture** — all captures were in May/June (summer mode).
   The winter heater detection logic is theoretically correct but not yet
   validated. A capture during simultaneous space heating + DHW legionella
   cycle is needed to confirm.

5. **DpId 482/483** — web-sourced Status/Leistung Zusatzheizung. Absent from
   all captures. May appear on different firmware versions or during service mode.

6. **Options: rated heater power** — currently hardcoded to 3.0 kW in const.py.
   Should be exposed in the options flow alongside COP.

7. **Write support** — theoretically possible (0x56 frames observed); requires
   full write-format characterisation from targeted captures. See README for
   risk assessment.
