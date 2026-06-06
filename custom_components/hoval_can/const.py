"""Constants for the Hoval CAN integration."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory

DOMAIN = "hoval_can"
DEFAULT_PORT = 3113
RECONNECT_DELAY = 10      # seconds between reconnect attempts
FRAME_TIMEOUT = 30        # seconds of silence before considering stream stale

# ── Options ────────────────────────────────────────────────────────────────
CONF_COP = "cop"
DEFAULT_COP = 6.3         # Coefficient of Performance (thermal out / elec in)
COP_MIN = 1.0
COP_MAX = 15.0

# ── Protocol ──────────────────────────────────────────────────────────────
FRAME_SEP = b"\xff\x01"   # between frames
FRAME_END = b"\xff\x02"   # end-of-frame marker

CMD_READ_RESP_BE = 0x42   # Read response, big-endian value    ← data
CMD_READ_RESP_LE = 0x62   # Read response, little-endian       ← data (room unit)

# Groups carrying time-schedule data — skip for sensor decoding
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

NULL_SENTINELS: dict[str, set] = {
    "S8":  {-128},
    "U16": {0x8000},
    "S16": {-32768},
    "U32": {0x80000000, 0xFFFFFFFF},
    "S32": {-2147483648},
    "S64": {-9223372036854775808},
}

# ── Electric heater ────────────────────────────────────────────────────────
# Rated power of the Heizstab, verified empirically:
# 280 L × 4186 J/kg·K × 2.5 °C ÷ (17 min × 60 s) ≈ 2.9 kW ≈ 3 kW
HEATER_RATED_POWER_KW: float = 3.0

# Heat pump generator must be hotter than DHW by at least this margin
# for the heat pump to be doing the DHW heating (not the electric element)
HEATER_DETECTION_MARGIN: float = 5.0

# DatapointIds used by detection and energy calculation
DP_DHW_ACTUAL      = 4       # Warmwasser-Ist       S16 dec=1 °C
DP_DHW_SETPOINT    = 1004    # Warmwasser-Soll      S16 dec=1 °C
DP_STATUS_WW       = 2052    # Status Warmwasserregelung  U8 (8 = charging)
DP_HEAT_GEN        = 7       # Wärmeerzeuger-Ist    S16 dec=1 °C
DP_THERMAL_POWER   = 29051   # Current Heating Power U32 dec=1 kW

DHW_STATUS_CHARGING = 8      # DP_STATUS_WW value when DHW demand is active

# ── Dispatcher signal builders ─────────────────────────────────────────────
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
    key: str
    name: str
    typename: str
    decimal: int
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
    # ── Temperatures ──────────────────────────────────────────────────────
    HovalSensorDescription(0,   "outdoor_temp",
        "Outdoor Temperature",             "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(1,   "room_temp",
        "Room Temperature",                "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(2,   "flow_temp",
        "Flow Temperature",                "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(4,   "dhw_temp",
        "DHW Temperature",                 "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(7,   "heat_gen_temp",
        "Heat Generator Temperature",      "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(16,  "solar_storage_temp",
        "Solar Storage Temperature",       "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT,
        enabled_default=False),
    HovalSensorDescription(118, "circulation_temp",
        "Circulation Temperature",         "U16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT,
        enabled_default=False),

    # ── Thermal power & performance ───────────────────────────────────────
    HovalSensorDescription(30,  "heat_pump_power",
        "Heat Pump Power",                 "U8",  0, "%",  None, SC.MEASUREMENT,
        "mdi:heat-pump"),
    HovalSensorDescription(8,   "power_limit",
        "Power Limit",                     "S16", 1, "%",  None, SC.MEASUREMENT,
        "mdi:gauge"),
    HovalSensorDescription(20052, "compressor_modulation",
        "Compressor Modulation",           "U8",  0, "%",  None, SC.MEASUREMENT,
        "mdi:sine-wave"),
    HovalSensorDescription(29051, "current_heating_power",
        "Current Heating Power",           "U32", 1, "kW", SDC.POWER, SC.MEASUREMENT),
    HovalSensorDescription(23009, "total_wez_electrical_energy",
        "Total WEZ Electrical Energy",     "U32", 3, "MWh", SDC.ENERGY,
        SC.TOTAL_INCREASING),
    HovalSensorDescription(2080, "wez_switch_cycles",
        "WEZ Switch Cycles",               "U32", 0, "",   None, SC.TOTAL_INCREASING,
        "mdi:counter"),

    # ── Status ────────────────────────────────────────────────────────────
    HovalSensorDescription(2051, "status_heating_circuit",
        "Heating Circuit Status",          "U8",  0, "",   None, None, "mdi:radiator"),
    HovalSensorDescription(2052, "status_dhw",
        "DHW Status",                      "U8",  0, "",   None, None, "mdi:water-boiler"),
    HovalSensorDescription(2053, "status_heat_pump",
        "Heat Pump Status",                "U8",  0, "",   None, None, "mdi:heat-pump"),
    HovalSensorDescription(34,   "operating_status",
        "Operating Status",                "U8",  0, "",   None, None, "mdi:information"),
    HovalSensorDescription(20051, "wez_fa_status",
        "WEZ FA Status",                   "U8",  0, "",   None, None,
        "mdi:heat-pump-outline", EC.DIAGNOSTIC, enabled_default=False),
    HovalSensorDescription(20053, "wez_operating_message",
        "WEZ Operating Message",           "U8",  0, "",   None, None, "mdi:message"),
    HovalSensorDescription(20125, "wez_operational_flag",
        "WEZ Operational Flag",            "U8",  0, "",   None, None,
        "mdi:flag", EC.DIAGNOSTIC, enabled_default=False),
    HovalSensorDescription(23085, "emissions_test_active",
        "Emissions Test Active",           "U8",  0, "",   None, None,
        "mdi:test-tube", EC.DIAGNOSTIC, enabled_default=False),
    HovalSensorDescription(38012, "smart_grid_status",
        "Smart Grid Status",               "U8",  0, "",   None, None,
        "mdi:transmission-tower"),

    # ── Setpoints ─────────────────────────────────────────────────────────
    HovalSensorDescription(1001, "room_setpoint",
        "Room Setpoint",                   "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(1002, "flow_setpoint",
        "Flow Setpoint",                   "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(1004, "dhw_setpoint",
        "DHW Setpoint",                    "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(1007, "heat_gen_setpoint",
        "Heat Generator Setpoint",         "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(3051, "normal_room_temp",
        "Normal Room Temperature",         "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(3053, "economy_room_temp",
        "Economy Room Temperature",        "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(3054, "normal_room_temp_hc2",
        "Normal Room Temperature HC2",     "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT,
        enabled_default=False),
    HovalSensorDescription(5051, "normal_dhw_setpoint",
        "Normal DHW Setpoint",             "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(5086, "economy_dhw_setpoint",
        "Economy DHW Setpoint",            "U8",  0, "°C", SDC.TEMPERATURE, SC.MEASUREMENT),
    HovalSensorDescription(7036, "const_heating_flow",
        "Constant Heating Flow Setpoint",  "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT,
        enabled_default=False),
    HovalSensorDescription(7047, "const_cooling_flow",
        "Constant Cooling Flow Setpoint",  "S16", 1, "°C", SDC.TEMPERATURE, SC.MEASUREMENT,
        enabled_default=False),

    # ── Operating modes ───────────────────────────────────────────────────
    HovalSensorDescription(3032, "control_strategy",
        "Control Strategy",                "U8",  0, "",   None, None, "mdi:strategy"),
    HovalSensorDescription(3050, "heating_mode",
        "Heating Operating Mode",          "U8",  0, "",   None, None,
        "mdi:home-thermometer"),
    HovalSensorDescription(9075, "heat_pump_mode",
        "Heat Pump Operating Mode",        "U8",  0, "",   None, None, "mdi:heat-pump"),

    # ── Firmware extensions (not in official April 2026 Excel) ─────────────
    HovalSensorDescription(502,  "active_heating_program",
        "Active Heating Program",          "STR", 0, "",   None, None,
        "mdi:calendar-text"),
    HovalSensorDescription(4005, "circuit_name",
        "Heating Circuit Name",            "STR", 0, "",   None, None, "mdi:label"),
)

# Fast lookup: dp_id → description
SENSOR_BY_DPID: dict[int, HovalSensorDescription] = {
    s.dp_id: s for s in SENSOR_DESCRIPTIONS
}

# dpIds whose sensor uses RestoreEntity (hardware counters)
PERSISTENT_DPIDS: frozenset[int] = frozenset({23009})
