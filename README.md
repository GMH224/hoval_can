# Hoval CAN — Home Assistant Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
![Version](https://img.shields.io/badge/version-0.2.0-blue)
![HA min version](https://img.shields.io/badge/HA-2023.1%2B-green)

Local-push integration for Hoval heat pump systems equipped with the **WLAN Gateway**. Connects directly to the proprietary CAN-BUS TCP stream on port 3113 — no cloud, no Modbus module required. Strictly **read-only**: nothing is ever written to the bus.

---

## How it works

The Hoval WLAN gateway continuously broadcasts CAN-BUS frames on TCP port 3113. This integration connects to that stream, decodes every frame, and exposes the data as Home Assistant entities.

Frame format (reverse-engineered from binary captures):
```
[FF 01] [3B header] [2B unit_id] [1B command] [2B group_id]
        [2B datapoint_id] [value bytes] [FF 02]
```
Only command `0x42` (big-endian response) and `0x62` (little-endian response from the room display unit) carry sensor data. All other frame types are silently ignored.

---

## Requirements

| Requirement | Detail |
|---|---|
| Hardware | Hoval heat pump with **WLAN Gateway** module |
| Home Assistant | 2023.1 or newer |
| Network | HA must reach the gateway IP on TCP port 3113 |
| Modbus module | **Not needed** |

---

## Installation

### Via HACS (recommended)

1. HACS → **Integrations** → ⋮ → **Custom repositories**
2. Add `https://github.com/GMH224/hoval_can` — category **Integration**
3. Find **Hoval CAN** and click **Download**
4. Restart Home Assistant

### Manual

1. Copy `custom_components/hoval_can/` into your HA `custom_components/` directory
2. Restart Home Assistant

---

## Configuration

1. **Settings → Devices & Services → Add Integration → Hoval CAN**
2. Enter the **IP address** of your WLAN gateway
3. Leave port at **3113** unless your setup is non-standard
4. HA tests the connection; if successful the integration is created

All sensors start as **Unknown** and populate within ~60 seconds as the device broadcasts each value. Energy counters start at **0.0 kWh** on first install and accumulate from that point.

### COP setting (Options)

After setup, click **Configure** on the integration card to adjust the COP:

- **Settings → Devices & Services → Hoval CAN → Configure**
- Enter the COP value (default **6.3**, range 1.0–15.0)
- The integration reloads automatically; all accumulated energy totals are preserved

The COP is used to calculate heat pump electrical power and energy from the thermal output (DpId=29051). Use your heat pump's seasonal or measured COP. A higher COP means less estimated electrical consumption for the same thermal output.

---

## Entities

### Sensors

#### Temperatures (7 sensors)
| Name | DpId | Notes |
|---|---|---|
| Outdoor Temperature | 0 | Also received from room display unit (0x62) |
| Room Temperature | 1 | |
| Flow Temperature | 2 | |
| DHW Temperature | 4 | Domestic hot water actual |
| Heat Generator Temperature | 7 | Heat pump refrigerant cycle output |
| Solar Storage Temperature | 16 | Disabled by default |
| Circulation Temperature | 118 | Disabled by default; Unknown if sensor not fitted |

#### Thermal Power & Performance (5 sensors)
| Name | DpId | Notes |
|---|---|---|
| Heat Pump Power | 30 | % of rated capacity |
| Power Limit | 8 | Configured power cap % |
| Compressor Modulation | 20052 | % |
| Current Heating Power | 29051 | kW thermal output — key input for electrical calculations |
| WEZ Switch Cycles | 2080 | Cumulative compressor starts |

#### Electrical Power & Energy (6 sensors, new in v0.2)
| Name | Unit | Notes |
|---|---|---|
| **Heat Pump Electrical Power** | kW | `thermal_kW / COP` — instantaneous |
| **Heat Pump Electrical Energy** | kWh | Cumulative; persistent; 3 decimals |
| **Electric Heater Power** | kW | 3.0 kW when on, 0.0 when off |
| **Electric Heater Energy** | kWh | Cumulative; persistent; 3 decimals |
| **Total Electrical Power** | kW | HP + heater combined instantaneous |
| **Total Electrical Energy** | kWh | HP + heater combined cumulative; persistent; 3 decimals |

All three energy sensors are `state_class: total_increasing` and suitable for the **HA Energy Dashboard**.

#### Hardware Energy Counter (1 sensor)
| Name | DpId | Notes |
|---|---|---|
| Total WEZ Electrical Energy | 23009 | MWh; sourced directly from the device hardware counter |

#### Status (7 sensors)
| Name | DpId | Notes |
|---|---|---|
| Heating Circuit Status | 2051 | 0 = idle |
| DHW Status | 2052 | **8 = DHW charging** — key for heater detection |
| Heat Pump Status | 2053 | 0 = off, 1 = running |
| Operating Status | 34 | |
| WEZ Operating Message | 20053 | |
| Smart Grid Status | 38012 | |
| WEZ FA Status | 20051 | Diagnostic; disabled by default |

#### Setpoints (11 sensors)
Room Setpoint, Flow Setpoint, DHW Setpoint, Heat Generator Setpoint, Normal Room Temperature, Economy Room Temperature, Normal Room Temperature HC2 (disabled by default), Normal DHW Setpoint, Economy DHW Setpoint, Constant Heating Flow Setpoint (disabled by default), Constant Cooling Flow Setpoint (disabled by default).

#### Operating Modes (3 sensors)
Control Strategy, Heating Operating Mode, Heat Pump Operating Mode.

#### Firmware Extensions (2 sensors)
Active Heating Program (string, e.g. "Summer"), Heating Circuit Name (string, e.g. "Bodenheizung"). Not in the official April 2026 datapoint list.

### Binary Sensor (1)

| Name | Notes |
|---|---|
| **Electric Heater Active** | True when the electric DHW heater (Heizstab) is running |

---

## Electric heater detection

The electric immersion heater (Heizstab / Zusatzheizung) does **not** broadcast a status on the CAN-BUS stream. Its state is derived from stream data.

**Detection logic — ON when all three are true:**

1. `DHW Status == 8` — system is actively charging the DHW tank
2. `DHW Temperature < DHW Setpoint` — target has not been reached
3. `Heat Generator Temperature ≤ DHW Temperature + 5 °C` — the heat pump's generator is not hot enough to heat the DHW tank; only the electric element can raise it

**Winter safety:** In winter the heat pump may run for space heating at 40 °C flow temperature. A DHW tank at 55 °C+ is already hotter than the generator, so the heat pump physically cannot heat it. Condition 3 fires correctly regardless of what the heat pump is doing for space heating.

**Verification:** Confirmed against binary captures of a full 62 °C DHW heating cycle (June 2026). The heat pump ran DHW to ~52 °C, then stopped. DHW continued rising at ~2.5 °C / 17 min → implied power ~2.9 kW ≈ 3 kW electric element confirmed.

---

## Electrical energy calculation

### Heat pump

```
elec_power_kW = thermal_power_kW / COP
elec_energy   += elec_power_kW × elapsed_hours   (left Riemann sum)
```

DpId=29051 updates approximately every 2 seconds, so the integration is highly accurate. If DpId=29051 returns None (connection lost or null sentinel), the running period is abandoned so no spurious energy accumulates across reconnections.

### Electric heater

```
elec_power_kW  = 3.0 kW  (when heater is active)
elec_energy    += 3.0 × elapsed_hours
```

### Total electrical

```
total_power_kW  = HP_elec_power + heater_power
total_energy    += total_power_kW × elapsed_hours
```

The total energy counter is independent — not a sum of the two sub-counters — so it persists and restores correctly on its own.

---

## Persistence and HA Energy Dashboard

The following sensors survive HA restarts via `RestoreEntity`:

| Sensor | Restore behaviour |
|---|---|
| Total WEZ Electrical Energy | Restores last known value; refreshed from device within ~60 s |
| Heat Pump Electrical Energy | Restores accumulated total; continues from where it left off |
| Electric Heater Energy | Restores accumulated total; continues from where it left off |
| Total Electrical Energy | Restores accumulated total; continues from where it left off |

To add to the Energy Dashboard: **Settings → Energy → Individual devices** → add each energy sensor.

---

## Limitations

- **Read-only.** No setpoints or commands are written to the bus.
- Modbus TCP (port 502) is not available with the WLAN gateway — only the CAN-BUS stream (port 3113) is exposed.
- The COP value is fixed per configuration. The actual instantaneous COP varies with outdoor temperature, load, and defrost cycles. Using a seasonal average COP gives a reasonable cumulative estimate but will be inaccurate for individual short periods.
- Electric heater energy starts from zero on first install. Historical energy before installation is not recoverable.
- Firmware-extension DatapointIds (502, 4005, etc.) may change between Hoval firmware versions.

---

## Tested with

- Hoval TopTronic E heat pump (WEZ), floor heating + DHW
- WLAN Gateway module (firmware observed May–June 2026)
- Home Assistant 2024.x

---

## Changelog

### v0.2.0
- Added **Heat Pump Electrical Power** and **Heat Pump Electrical Energy** sensors (derived via COP)
- Added **Total Electrical Power** and **Total Electrical Energy** sensors
- Added **Options flow**: configure COP via the HA UI without reinstalling
- COP default: 6.3, range 1.0–15.0
- All three electrical energy sensors are persistent (RestoreEntity, TOTAL_INCREASING)
- Integration reloads automatically when COP is changed; energy totals are preserved

### v0.1.1
- Fixed `Total WEZ Electrical Energy` (DpId=23009) persistence across restarts
- Fixed `Electric Heater Energy` always starting at 0.0 (not Unknown) on first install
- Fixed heater energy holding last value (not reverting to Unknown) when source dpIds temporarily unavailable
- Bumped version to 0.1.1

### v0.1.0
- Initial release
- 40 datapoint-based sensors (temperatures, status, setpoints, operating modes)
- Electric Heater Active binary sensor
- Electric Heater Power and Electric Heater Energy sensors
- IP address configured via HA setup flow

---

## License

MIT
