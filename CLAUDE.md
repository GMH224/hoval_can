# CLAUDE.md — Hoval CAN Integration: Developer Context

This file documents the reverse-engineering work, protocol findings, and
architectural decisions so that future development (including AI-assisted)
can pick up without starting over.

---

## What this integration does

Connects to a Hoval heat pump's WLAN gateway on TCP port 3113 and decodes
the proprietary CAN-BUS stream. Exposes ~43 sensors and 1 binary sensor in
Home Assistant. Strictly read-only — no data is ever written to the bus.

---

## Hardware

- **Device**: Hoval TopTronic E heat pump (WEZ module) with floor heating (HKW) and DHW
- **Gateway**: Hoval WLAN Gateway module (article number varies)
- **Interface**: TCP port 3113 — proprietary binary CAN-BUS stream
- **Not available**: Modbus TCP (port 502) requires a separate hardware module

---

## Protocol (reverse-engineered from binary captures, May–June 2026)

### Frame structure

```
[FF 01]  ← frame separator (between frames)
[byte 0–2]   3-byte routing header  (usually 00 00 00)
[byte 3–4]   2-byte unit ID, big-endian  (e.g. 0x0201 = 513)
[byte 5]     1-byte command code
[byte 6–7]   2-byte group ID, big-endian
[byte 8–9]   2-byte DatapointId, big-endian
[byte 10+]   value bytes (length determined by TypeName)
[FF 02]  ← end of frame
```

### Command codes

| Code | Name | Action |
|---|---|---|
| 0x40 | READ-REQUEST | Master polls device — ignore |
| **0x42** | **READ-RESPONSE BE** | **Device returns value — decode this** |
| 0x44 | REQ-VARIANT | Alternate request — ignore |
| 0x56 | WRITE / BOUNDS | Write command or min/max response — ignore |
| 0x61 | ERROR/NAK | Ignore |
| **0x62** | **READ-RESPONSE LE** | **Value from room display (little-endian) — decode this** |
| 0x70 | PUSH-MULTI | Compact status block — not decoded in v0.1 |
| 0x74 | PUSH-NULL | Null status block — ignore |

### 0x42 decoding (standard)

```
value_bytes = frame[10:]
decoded = struct.unpack('>' + fmt, value_bytes[:nbytes])[0] / 10^decimal
```

### 0x62 decoding (little-endian, from room display unit 0x2201)

```
# frame[10] = status byte (0x00 = OK)
# frame[11:13] = 2-byte little-endian value
value_bytes = frame[11:13]
decoded = struct.unpack('<h', value_bytes)[0] / 10^decimal
```

The 0x62 command only carries data when `group_id & 0x8000` (high bit set = response).
Low-bit group = request frame, no data.

### TypeName → byte length

| TypeName | Bytes | struct fmt |
|---|---|---|
| U8 / S8 | 1 | B / b |
| U16 / S16 / LIST | 2 | H / h / H |
| U32 / S32 | 4 | I / i |
| S64 | 8 | q |

### Null sentinels (value means "not connected" / error)

| Type | Null value |
|---|---|
| S8 | -128 |
| U16 | 0x8000 (bleeds from S16 convention) |
| S16 | -32768 (0x8000) |
| U32 | 0x80000000 or 0xFFFFFFFF |
| S32 | -2147483648 |
| All | all-0xFF bytes |

### Groups to skip

Groups 15614, 15922, 15923, 15924 carry time-schedule data with a
complex sub-structure. DatapointIds 64000–64050 carry single-byte time
slot values (N × 10 min from midnight). Both are decoded by the standalone
reader script but not exposed as HA entities in v0.1.

---

## DatapointId reference

The DatapointId in the CAN-BUS stream corresponds to the "DatapointId"
column in the official Hoval Excel file
`Hoval_Modbus_Datenpunktliste_Stand_April_2026.xlsx`.

**Important**: The "Register Address" column in that Excel is the MODBUS
register number — a completely different address space. Do not confuse them.

### Ambiguous DatapointIds

Some DatapointIds appear under multiple unit types (e.g. FW and WEZ) with
**different TypeNames**. The coordinator's loader prefers the entry with the
smallest UnitId (WEZ UnitIds 1–8 beat FW UnitIds 193+) because the captured
data matched WEZ TypeNames.

### Firmware extension DatapointIds

The following DatapointIds appear in captures but are absent from the
April 2026 official Excel. They are probably firmware-internal and may
change between firmware versions:

| DatapointId | Observed content | Best guess |
|---|---|---|
| 500 | U8 = 4 | Heating program count or index |
| 502, 505 | ASCII string "Summer" | Active heating program name |
| 503, 504 | U8 = 0/1 | Program flags |
| 2010, 2011, 2018 | S16 = 0 | Unknown status/counter |
| 3054 | S16, °C | Normal room temp for 2nd heating circuit |
| 3058, 3078 | S16 | Unknown HC setpoints |
| 4005 | ASCII string "Bodenheizung" etc. | Circuit name |
| 7014 | U8 = 3 | WEZ status flag |
| 20125 | U8 = 0/1 | WEZ operational flag |

---

## Electric heater detection

### Why there is no direct datapoint

DatapointId 1029 (`Betriebsst. Zusatzhzg.`, operating hours counter) and
DpId 482/483 (web-sourced status/power, unverified) were searched across
18,000+ frames including a full 62 °C DHW heating cycle. Neither appeared.
The electric heater does not broadcast its state on the CAN-BUS stream
available via the WLAN gateway.

### Derived detection algorithm

Located in `coordinator.py` → `electric_heater_on` property.

```python
heater_on = (
    status_ww == 8              # DHW demand active (DpId 2052)
    and dhw_actual < dhw_sp     # target not reached (DpId 4 < DpId 1004)
    and heat_gen <= dhw_actual + HEATER_DETECTION_MARGIN  # DpId 7 ≤ DpId 4 + 5°C
)
```

### Why condition 3 works

The heat pump can only heat the DHW tank if its generator temperature is
ABOVE the tank temperature. The moment the heat pump's generator cools to
within 5 °C of the DHW temperature, thermodynamics prevents it from
contributing heat to the tank. Only the electric element can continue.

**Summer example (from captures):**
- Heat pump runs DHW to 52 °C, generator at 58 °C → condition 3 FALSE → heater OFF
- Heat pump stops; generator cools to 30 °C, DHW still 52 °C → condition 3 TRUE → heater ON ✓

**Winter example (theoretical, not yet captured):**
- Heat pump runs for space heating at 42 °C flow temp; DHW at 58 °C, target 62 °C
- Generator (42 °C) ≤ DHW (58 °C) + 5 °C → condition 3 TRUE → heater ON ✓

### Rated power

3.0 kW, verified empirically:
`280 L × 4186 J/(kg·K) × 2.5 °C ÷ (17 min × 60 s) = 2.88 kW ≈ 3 kW`

Change `HEATER_RATED_POWER_KW` in `const.py` if your unit has a different
element (Hoval offers 2, 3, and 6 kW variants).

### Energy persistence

`HovalElectricHeaterEnergySensor` in `sensor.py` uses HA's `RestoreEntity`
(`async_get_last_state`) to reload the accumulated kWh across restarts.
The counter is `TOTAL_INCREASING` and never resets.

Timing precision: start time is recorded on heater-on; energy is flushed on
heater-off and on entity removal. A 60-second interval timer updates the
displayed value while the heater is running. Any energy accumulated in the
period between the last HA state write and an unexpected restart is lost
(typically < 0.05 kWh per event).

---

## Architecture

```
__init__.py          Setup / teardown, forward to platforms
config_flow.py       UI: enter host + port, test TCP connection
coordinator.py       TCP reader, frame parser, data store, dispatcher signals
sensor.py            Standard dpId sensors + derived heater power/energy sensors
binary_sensor.py     Electric heater active binary sensor
const.py             All constants: sensor definitions, protocol constants,
                     heater config
strings.json         Config flow UI strings
translations/en.json Same content (English)
```

### Dispatcher signals

| Signal | Fired when |
|---|---|
| `hoval_can_{entry_id}_dp_{dp_id}` | A specific DatapointId updates |
| `hoval_can_{entry_id}_electric_heater` | Electric heater on/off changes |
| `hoval_can_{entry_id}_connection` | TCP connection established or lost |

---

## Standalone reader script

`hoval_raw_reader.py` (v4) is the companion Python script used during
reverse engineering. It connects to port 3113 and logs decoded values to
a file. It also supports replaying captured `.bin` files.

Key commands:
```bash
# Live capture for 75 minutes
python hoval_raw_reader.py --ip 192.168.7.23 \
    --datapoints "Hoval_Modbus_Datenpunktliste_Stand_April_2026.xlsx" \
    --duration 75 --outdir ./capture

# Replay a previous capture
python hoval_raw_reader.py --replay hoval_raw_20260603_090529.bin \
    --datapoints "Hoval_Modbus_Datenpunktliste_Stand_April_2026.xlsx" \
    --stats-interval 0 --no-log

# Look up a Modbus register address
python hoval_raw_reader.py \
    --datapoints "Hoval_Modbus_Datenpunktliste_Stand_April_2026.xlsx" \
    --lookup-reg 19484
```

---

## Known gaps / future work

1. **0x70 PUSH-MULTI frames** — appear 80+ times per 500 frames with compact
   status blocks (grp 0xA100, 0xA300, 0xA600). Not decoded in v0.1. Structure:
   `[count_byte][count × U8 values]` — likely per-circuit status arrays.

2. **LIST type text mapping** — dpIds 3050, 9075 return integer codes.
   Text values (e.g. "Woche 1", "Standby") are in the Excel Text 0..N columns
   but not loaded in v0.1. A future version could expose these as select
   entities for read display.

3. **Winter capture** — all development captures were in late May / early June
   (summer mode). The electric heater detection winter logic is theoretically
   correct but not yet validated against a winter capture. A capture during
   active space heating + simultaneous DHW legionella cycle is needed.

4. **DpId 482 / 483** — web-sourced `Status Zusatzheizung` / `Leistung
   Zusatzheizung`. Not verified for this device. If they appear in a future
   winter capture, replace the derived detection with the direct dpId.

5. **Options flow** — allow changing `HEATER_RATED_POWER_KW` via the HA UI
   without editing source files.

6. **Device auto-detection** — the unit type (WEZ model, kW rating) could
   potentially be read from the serial number datapoints.

---

## Capture files used during development

| File | Frames | Context |
|---|---|---|
| hoval_raw_20260531_074801.bin | 503 | Morning, summer, heat pump idle |
| hoval_raw_20260531_183046.bin | ~2000 | Evening, summer, DHW legionella cycle started |
| hoval_decoded_20260531_233008.log | 5000 | Night, first large capture |
| hoval_raw_20260603_090529.bin | 14594 | DHW 62 °C heating cycle (heat pump phase) |
| hoval_raw_20260603_095039.bin | 4319 | DHW 62 °C heating cycle (electric heater phase) |
