# Hoval CAN — Home Assistant Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
![Version](https://img.shields.io/badge/version-0.1.0-blue)
![HA min version](https://img.shields.io/badge/HA-2023.1%2B-green)

Local-push integration for Hoval heat pump systems equipped with the **WLAN Gateway**. Connects directly to the proprietary CAN-BUS TCP stream on port 3113 — no cloud, no Modbus module required.

---

## How it works

The Hoval WLAN gateway continuously broadcasts CAN-BUS frames on TCP port 3113. This integration connects to that stream, decodes every frame using the reverse-engineered protocol, and exposes the data as Home Assistant entities. Nothing is ever written back to the bus — the integration is **strictly read-only**.

Frame format (reverse-engineered from captures):
```
[FF 01] [3B header] [2B unit_id] [1B command] [2B group_id] [2B datapoint_id] [value bytes] [FF 02]
```
Only command `0x42` (big-endian response) and `0x62` (little-endian response from the room display unit) carry sensor data.

---

## Requirements

| Requirement | Detail |
|---|---|
| Hardware | Hoval heat pump with **WLAN Gateway** (port 3113) |
| Home Assistant | 2023.1 or newer |
| Network | HA must be able to reach the gateway's IP on port 3113 |
| Modbus module | **Not needed** |

---

## Installation

### Via HACS (recommended)

1. In HACS → **Integrations** → ⋮ → **Custom repositories**
2. Add `https://github.com/GMH224/hoval_can` — category **Integration**
3. Find **Hoval CAN** and click **Download**
4. Restart Home Assistant

### Manual

1. Copy the `custom_components/hoval_can/` folder into your HA config's `custom_components/` directory
2. Restart Home Assistant

---

## Configuration

1. **Settings → Devices & Services → Add Integration → Hoval CAN**
2. Enter the **IP address** of your WLAN gateway
3. Leave the port at **3113** unless you have a non-standard setup
4. HA will test the connection; if it succeeds the integration is created

All sensors start as **Unknown** and populate as the device broadcasts each value. Most sensors update within the first 30–60 seconds.

---

## Entities

### Sensors (43 total)

#### Temperatures
| Name | DatapointId | Notes |
|---|---|---|
| Outdoor Temperature | 0 | Also received from room display unit |
| Room Temperature | 1 | |
| Flow Temperature | 2 | |
| DHW Temperature | 4 | Domestic hot water actual |
| Heat Generator Temperature | 7 | Heat pump refrigerant cycle |
| Solar Storage Temperature | 16 | Disabled by default |
| Circulation Temperature | 118 | Disabled by default; null if sensor not fitted |

#### Power & Performance
| Name | DatapointId | Notes |
|---|---|---|
| Heat Pump Power | 30 | % of rated capacity |
| Power Limit | 8 | Configured power cap % |
| Compressor Modulation | 20052 | % |
| Current Heating Power | 29051 | kW thermal output |
| Total WEZ Electrical Energy | 23009 | MWh, cumulative electrical input to heat pump |
| WEZ Switch Cycles | 2080 | Cumulative compressor start count |

#### Status
| Name | DatapointId | Notes |
|---|---|---|
| Heating Circuit Status | 2051 | 0 = idle |
| DHW Status | 2052 | **8 = DHW charging** (key for heater detection) |
| Heat Pump Status | 2053 | 0 = off, 1 = running |
| Operating Status | 34 | |
| WEZ Operating Message | 20053 | |
| Smart Grid Status | 38012 | |

#### Setpoints
| Name | DatapointId | Notes |
|---|---|---|
| Room Setpoint | 1001 | °C |
| Flow Setpoint | 1002 | °C |
| DHW Setpoint | 1004 | °C |
| Heat Generator Setpoint | 1007 | °C |
| Normal Room Temperature | 3051 | °C |
| Economy Room Temperature | 3053 | °C |
| Normal Room Temperature HC2 | 3054 | °C, disabled by default |
| Normal DHW Setpoint | 5051 | °C |
| Economy DHW Setpoint | 5086 | °C |
| Constant Heating Flow Setpoint | 7036 | °C, disabled by default |
| Constant Cooling Flow Setpoint | 7047 | °C, disabled by default |

#### Operating Modes
| Name | DatapointId | Notes |
|---|---|---|
| Control Strategy | 3032 | Integer code |
| Heating Operating Mode | 3050 | Integer code |
| Heat Pump Operating Mode | 9075 | Integer code |

#### Firmware Extensions *(not in official datapoint list)*
| Name | DatapointId | Notes |
|---|---|---|
| Active Heating Program | 502 | String, e.g. "Summer" |
| Heating Circuit Name | 4005 | String, e.g. "Bodenheizung" |

### Electric Heater (derived — no direct CAN-BUS datapoint)

The electric immersion heater (Heizstab / Zusatzheizung) does **not** broadcast a status on the CAN-BUS stream. Its state is derived from the available stream data.

| Entity | Type | Description |
|---|---|---|
| **Electric Heater Active** | Binary sensor | True when heater is running |
| **Electric Heater Power** | Sensor (kW) | 3.0 kW when active, 0.0 when idle |
| **Electric Heater Energy** | Sensor (kWh) | Cumulative energy; **persists across restarts** |

#### Detection logic

The heater is detected as **ON** when all three conditions are true:

1. `DHW Status == 8` — the system is actively trying to charge the DHW tank
2. `DHW Temperature < DHW Setpoint` — the target has not been reached yet
3. `Heat Generator Temperature ≤ DHW Temperature + 5 °C` — the heat pump's generator is not hot enough to heat the DHW tank; only the electric element can raise it

**Why condition 3 is winter-safe:** In winter the heat pump may run for space heating at 40 °C flow temperature. A DHW tank at 55 °C is already hotter than the generator, so the heat pump physically cannot heat it. The electric heater must be active — and condition 3 correctly triggers.

#### Energy calculation

```
Electric Heater Energy += 3.0 kW × elapsed_hours_heater_on
```

Rated power (3.0 kW) was verified empirically from captures:
`280 L × 4186 J/kg·K × 2.5 °C ÷ (17 min × 60 s) ≈ 2.9 kW ≈ 3 kW`

The energy counter is `state_class: total_increasing` and is backed by HA's `RestoreEntity`, so it survives restarts. Any partial period at the moment of a restart is not counted (typically < 0.1 kWh per event).

To change the rated power, edit `HEATER_RATED_POWER_KW` in `const.py`.

---

## Tested with

- Hoval TopTronic E heat pump (WEZ) with floor heating and DHW
- WLAN Gateway module, firmware observed May–June 2026
- Home Assistant 2024.x

---

## Limitations

- **Read-only.** No setpoints or commands can be written.
- The Modbus register address space (port 502) is **not** accessible with the WLAN gateway; only the CAN-BUS stream (port 3113) is available.
- Datapoints not present in the official April 2026 datapoint list are marked as firmware extensions and may change between firmware versions.
- The electric heater energy counter starts from zero on first install. Historical energy is not recoverable.

---

## License

MIT
