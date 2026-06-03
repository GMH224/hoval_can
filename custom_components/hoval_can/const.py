"""Constants for the Hoval CAN integration."""
from __future__ import annotations

from dataclasses import dataclass, field

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory

DOMAIN = "hoval_can"
DEFAULT_PORT = 3113
RECONNECT_DELAY = 10          # seconds between reconnect attempts
FRAME_TIMEOUT = 30            # seconds before considering stream stale

# ── Protocol ──────────────────────────────────────────────────────────────
FRAME_SEP = b"\xff\x01"       # between frames in the TCP stream
FRAME_END = b"\xff\x02"       # end-of-frame marker

CMD_READ_RESP_BE = 0x42       # Read response, big-endian value
CMD_READ_RESP_LE = 0x62       # Read response, little-endian (from room display)

# Group IDs that carry time-schedule data – skip them for sensor decoding
SCHEDULE_GROUPS = {15614, 15922, 15923, 15924}

# ── Type system ───────────────────────────────────────────────────────────
TBYTES: dict[str, int] = {
    "U8": 1, "S8": 1,
    "U16": 2, "S16": 2, "LIST": 2,
    "U32": 4, "S32": 4,
    "S64": 8,
}

STRUCT_FMT: dict[str, str] = {
    "U8": "B", "S8": "b",
    "U16": "H", "S16": "h", "LIST": "H",
    "U32": "I", "S32": "i",
    "S64": "q",
}

# Raw integer values that represent "not connected" / error
NULL_SENTINELS: dict[str, set] = {
    "S8":  {-128},
    "U16": {0x8000},           # bleeds from S16 convention on some sensors
    "S16": {-32768},
    "U32": {0x80000000, 0xFFFFFFFF},
    "S32": {-2147483648},
    "S64": {-9223372036854775808},
}

# ── Electric heater configuration ─────────────────────────────────────────
# Rated power of the Heizstab (electric immersion heater) in kW.
# Verified empirically: 280 L tank × 4186 J/kg·K × 2.5 °C / (17 min × 60 s) ≈ 2.9 kW
HEATER_RATED_POWER_KW: float = 3.0

# Detection margin (°C): electric heater is active when the heat generator
# temperature is no more than this margin above the DHW temperature.
# Rationale: if the heat pump were heating the DHW, its generator must be
# HOTTER than the water.  When generator ≤ DHW + margin, only the electric
# element can be raising the DHW temperature.  Works in winter too: even if
# the heat pump is running for space heating (e.g. 40 °C flow), it cannot
# simultaneously heat a 55 °C DHW tank – the electric heater must be doing it.
HEATER_DETECTION_MARGIN: float = 5.0

# DpId numbers used by the electric-heater detection algorithm
DP_DHW_ACTUAL   = 4      # Warmwasser-Ist SF       S16 dec=1 °C
DP_DHW_SETPOINT = 1004   # Warmwasser-Soll         S16 dec=1 °C
DP_STATUS_WW    = 2052   # Status Warmwasserregelung U8  (8 = DHW charging)
DP_HEAT_GEN     = 7      # Wärmeerzeuger-Ist        S16 dec=1 °C
DHW_STATUS_CHARGING = 8  # value of DP_STATUS_WW when DHW demand is active

# ── Dispatcher signal helpers ─────────────────────────────────────────────
def dp_signal(entry_id: str, dp_id: int) -> str:
    return f"{DOMAIN}_{entry_id}_dp_{dp_id}"

def heater_signal(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_electric_heater"

def connection_signal(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_connection"

# ── Sensor descriptions ───────────────────────────────────────────────────
@dataclass(frozen=True)
class HovalSensorDescription:
    dp_id: int
    key: str                    # used for entity_id and unique_id suffix
    name: str                   # English display name
    typename: str               # U8 / S16 / U16 / U32 / S32 / LIST / STR
    decimal: int                # scale: raw_int / 10^decimal = value
    unit: str
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None
    icon: str | None = None
    entity_category: EntityCategory | None = None
    enabled_default: bool = True


SDC = SensorDeviceClass
SC  = SensorStateClass
EC  = EntityCategory

SENSOR_DESCRIPTIONS: tuple[HovalSensorDescription, ...] = (
    # ── Temperatures ───────────────────────────────────────────────────────
    HovalSensorDescription(0,   "outdoor_temp",
        "Outdoor Temperature",          "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(1,   "room_temp",
        "Room Temperature",             "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(2,   "flow_temp",
        "Flow Temperature",             "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(4,   "dhw_temp",
        "DHW Temperature",              "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(7,   "heat_gen_temp",
        "Heat Generator Temperature",   "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(16,  "solar_storage_temp",
        "Solar Storage Temperature",    "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT,
        enabled_default=False),
    HovalSensorDescription(118, "circulation_temp",
        "Circulation Temperature",      "U16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT,
        enabled_default=False),

    # ── Power / Performance ────────────────────────────────────────────────
    HovalSensorDescription(30,  "heat_pump_power",
        "Heat Pump Power",              "U8",  0, "%",  None, SC.MEASUREMENT,
        "mdi:heat-pump"),
    HovalSensorDescription(8,   "power_limit",
        "Power Limit",                  "S16", 1, "%",  None, SC.MEASUREMENT,
        "mdi:gauge"),
    HovalSensorDescription(20052, "compressor_modulation",
        "Compressor Modulation",        "U8",  0, "%",  None, SC.MEASUREMENT,
        "mdi:sine-wave"),
    HovalSensorDescription(29051, "current_heating_power",
        "Current Heating Power",        "U32", 1, "kW", SDC.POWER, SC.MEASUREMENT),
    HovalSensorDescription(23009, "total_wez_electrical_energy",
        "Total WEZ Electrical Energy",  "U32", 3, "MWh", SDC.ENERGY,
        SC.TOTAL_INCREASING),
    HovalSensorDescription(2080, "wez_switch_cycles",
        "WEZ Switch Cycles",            "U32", 0, "",   None, SC.TOTAL_INCREASING,
        "mdi:counter"),

    # ── Status ─────────────────────────────────────────────────────────────
    HovalSensorDescription(2051, "status_heating_circuit",
        "Heating Circuit Status",       "U8",  0, "",   None, None, "mdi:radiator"),
    HovalSensorDescription(2052, "status_dhw",
        "DHW Status",                   "U8",  0, "",   None, None, "mdi:water-boiler"),
    HovalSensorDescription(2053, "status_heat_pump",
        "Heat Pump Status",             "U8",  0, "",   None, None, "mdi:heat-pump"),
    HovalSensorDescription(34,   "operating_status",
        "Operating Status",             "U8",  0, "",   None, None, "mdi:information"),
    HovalSensorDescription(20051, "wez_fa_status",
        "WEZ FA Status",                "U8",  0, "",   None, None,
        "mdi:heat-pump-outline", EC.DIAGNOSTIC, enabled_default=False),
    HovalSensorDescription(20053, "wez_operating_message",
        "WEZ Operating Message",        "U8",  0, "",   None, None, "mdi:message"),
    HovalSensorDescription(20125, "wez_operational_flag",
        "WEZ Operational Flag",         "U8",  0, "",   None, None,
        "mdi:flag", EC.DIAGNOSTIC, enabled_default=False),
    HovalSensorDescription(23085, "emissions_test_active",
        "Emissions Test Active",        "U8",  0, "",   None, None,
        "mdi:test-tube", EC.DIAGNOSTIC, enabled_default=False),
    HovalSensorDescription(38012, "smart_grid_status",
        "Smart Grid Status",            "U8",  0, "",   None, None,
        "mdi:transmission-tower"),

    # ── Setpoints ──────────────────────────────────────────────────────────
    HovalSensorDescription(1001, "room_setpoint",
        "Room Setpoint",                "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(1002, "flow_setpoint",
        "Flow Setpoint",                "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(1004, "dhw_setpoint",
        "DHW Setpoint",                 "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(1007, "heat_gen_setpoint",
        "Heat Generator Setpoint",      "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(3051, "normal_room_temp",
        "Normal Room Temperature",      "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(3053, "economy_room_temp",
        "Economy Room Temperature",     "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(3054, "normal_room_temp_hc2",
        "Normal Room Temperature HC2",  "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT,
        enabled_default=False),
    HovalSensorDescription(5051, "normal_dhw_setpoint",
        "Normal DHW Setpoint",          "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(5086, "economy_dhw_setpoint",
        "Economy DHW Setpoint",         "U8",  0, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(7036, "const_heating_flow",
        "Constant Heating Flow Setpoint","S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT,
        enabled_default=False),
    HovalSensorDescription(7047, "const_cooling_flow",
        "Constant Cooling Flow Setpoint","S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT,
        enabled_default=False),

    # ── Operating modes ────────────────────────────────────────────────────
    HovalSensorDescription(3032, "control_strategy",
        "Control Strategy",             "U8",  0, "",   None, None, "mdi:strategy"),
    HovalSensorDescription(3050, "heating_mode",
        "Heating Operating Mode",       "U8",  0, "",   None, None,
        "mdi:home-thermometer"),
    HovalSensorDescription(9075, "heat_pump_mode",
        "Heat Pump Operating Mode",     "U8",  0, "",   None, None, "mdi:heat-pump"),

    # ── Firmware extensions (not in official April 2026 Excel) ─────────────
    HovalSensorDescription(502,  "active_heating_program",
        "Active Heating Program",       "STR", 0, "",   None, None,
        "mdi:calendar-text"),
    HovalSensorDescription(4005, "circuit_name",
        "Heating Circuit Name",         "STR", 0, "",   None, None, "mdi:label"),
)

# Build fast lookup dp_id → description
SENSOR_BY_DPID: dict[int, HovalSensorDescription] = {
    s.dp_id: s for s in SENSOR_DESCRIPTIONS
}
