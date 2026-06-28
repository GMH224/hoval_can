"""Constants for the Hoval CAN integration."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory

DOMAIN = "hoval_can"
DEFAULT_PORT      = 3113
RECONNECT_DELAY   = 10    # s between reconnect attempts (initial / floor)
RECONNECT_DELAY_MAX = 120 # s — cap for exponential reconnect backoff
RECONNECT_BACKOFF = 2.0   # exponential growth factor between attempts
FRAME_TIMEOUT     = 15    # s per read() poll — how often the watchdog wakes up
STALE_TIMEOUT     = 90    # s without ANY received bytes → force reconnect
                          # (frames normally arrive ~2 s apart; 90 s is well
                          #  beyond any legitimate quiet period)
DATA_STALE_TIMEOUT = 300  # s without a DECODABLE datapoint → force reconnect.
                          # Catches a socket that is alive and streaming bytes
                          # but no longer yielding usable frames (corruption /
                          # protocol desync) — bytes alone would fool the
                          # byte-level watchdog above.

# ── RX framing safety ──────────────────────────────────────────────────────
# Frames are tens of bytes and the separator recurs ~every 2 s, so the working
# buffer stays small. If the separator never appears (garbage / desync) the
# buffer must not grow without bound — that would be a memory-exhaustion DoS.
MAX_RX_BUFFER  = 65536    # bytes — hard cap on the un-split RX buffer
RX_RESYNC_KEEP = 1024     # bytes retained after an overflow to allow resync

# ── Windowed telemetry rates (sliding window over periodic snapshots) ───────
# A bounded ring of (timestamp, framing_errors, decoded_count) snapshots taken
# every RATE_SAMPLE_INTERVAL feeds two derived rates. Cumulative counters are
# retained underneath for exporters / HA's own Derivative & Statistics helpers.
RATE_SAMPLE_INTERVAL  = 60     # s between rate snapshots
THROUGHPUT_WINDOW_S   = 3600   # s (60 min) window for decoded-datapoints/min
ERROR_RATE_WINDOW_S   = 900    # s (15 min) window for framing-errors/hour
RATE_MIN_ELAPSED_S    = 120    # s minimum span before a rate is reported
                               # (else None/"unknown" during warm-up)

# ── TCP keep-alive (defense in depth: lets the OS surface a dead peer) ──────
# asyncio.open_connection() does NOT enable SO_KEEPALIVE by default, so a
# half-open connection (gateway reboot / Wi-Fi drop with no FIN/RST) would
# otherwise rely on the kernel default of ~2 h. These tune it aggressively.
TCP_KEEPALIVE_IDLE     = 30   # s idle before first keepalive probe
TCP_KEEPALIVE_INTERVAL = 10   # s between probes
TCP_KEEPALIVE_COUNT    = 3    # failed probes before the socket is declared dead

# ── Configurable options ───────────────────────────────────────────────────
# COP is calculated dynamically from live sensor data — not user-configurable.
CONF_HEATER_POWER     = "heater_power_kw"
DEFAULT_HEATER_POWER_KW: float = 3.0   # Rated power of the electric DHW heater
HEATER_POWER_MIN      = 0.5            # kW
HEATER_POWER_MAX      = 12.0           # kW  (dual-element upper bound)

# Passive ("free"/natural) cooling: the compressor is OFF and only the
# circulation pump(s) run, so the draw is small and roughly constant. Configured
# in WATTS (not kW) to match the small magnitudes involved.
CONF_COOLING_POWER       = "cooling_power_w"
DEFAULT_COOLING_POWER_W: float = 100.0  # W — typical circulation-pump draw
COOLING_POWER_MIN        = 0.0          # W
COOLING_POWER_MAX        = 500.0        # W

# ── CAN-BUS protocol ──────────────────────────────────────────────────────
FRAME_SEP = b"\xff\x01"
FRAME_END = b"\xff\x02"

CMD_READ_RESP_BE = 0x42   # Big-endian response    ← data
CMD_READ_RESP_LE = 0x62   # Little-endian response ← data (room display)

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

# ── Electric heater detection ──────────────────────────────────────────────
HEATER_DETECTION_MARGIN: float = 5.0   # °C

# Compressor is considered "running" above this modulation (%). Matches the
# calculate_cop() "not running" threshold (modulation <= 1.0). Used by the
# electric-heater detection: under DHW priority a running compressor is itself
# charging the tank, so the Heizstab is treated as off while modulation exceeds
# this value. The Heizstab is only detected once the compressor has stopped.
COMPRESSOR_RUNNING_MODULATION: float = 1.0   # %

# Key DatapointIds
DP_DHW_ACTUAL      = 4       # Warmwasser-Ist        S16 dec=1 °C
DP_DHW_SETPOINT    = 1004    # Warmwasser-Soll       S16 dec=1 °C
DP_STATUS_WW       = 2052    # Status WW             U8  (8 = charging)
DP_STATUS_HC       = 2051    # Heating Circuit Status U8 (9 = passive cooling)
DP_HEAT_GEN        = 7       # Wärmeerzeuger-Ist     S16 dec=1 °C  ← COP input
DP_THERMAL_POWER   = 29051   # Current Heating Power U32 dec=1 kW
DP_MODULATION      = 20052   # Compressor Modulation U8  %         ← COP input

DHW_STATUS_CHARGING = 8
HC_STATUS_PASSIVE_COOLING = 9   # status_heating_circuit value for passive cooling

# ── Dynamic COP formula ───────────────────────────────────────────────────
# Two-regime model using live temperature lift.
#
# COP inputs:
#   m      = compressor modulation %     (DpId 20052 → sensor.hoval_can_compressor_modulation)
#   t      = heat generator temperature °C (DpId 7 → sensor.hoval_can_heat_generator_temperature)
#
# Regime selection: t ≤ 40 °C → Space Heating;  t > 40 °C → DHW
#
# Lift correction: COP scales inversely with temperature lift (t − t_source).
#   Space heating reference lift : 17.5 °C  → t_gen reference = 30 °C
#   DHW reference lift            : 39.5 °C  → t_gen reference = 52 °C
#
# Guard-rails:
#   m ≤ 1  or  t ≤ t_source  →  return 0.0  (heat pump off / cold start)
#   Final COP clamped to [COP_MIN, COP_MAX]

COP_SOURCE_TEMP: float  = 12.5   # °C — heat source temperature (ground/air)
COP_SH_LIFT_REF: float  = 17.5   # °C — space heating reference lift
COP_DHW_LIFT_REF: float = 39.5   # °C — DHW reference lift
COP_SH_MAX_TGEN: float  = 40.0   # °C — T_gen threshold between regimes
COP_CLAMP_MIN: float    = 1.0
COP_CLAMP_MAX: float    = 8.5


def calculate_cop(modulation: float, heat_gen_temp: float) -> float:
    """Dynamic COP from compressor modulation and heat generator temperature.

    Two-regime piecewise model calibrated against real operating data.
    Faithfully implements the user-provided HA template formula.

    Space Heating (heat_gen_temp ≤ 40 °C):
        cop_base = 0.5833×m           if m < 12
                 = 7.0                if 12 ≤ m ≤ 22
                 = 7.988 − 0.0449×m  if m > 22
        COP = cop_base × (17.5 / lift)

    DHW (heat_gen_temp > 40 °C):
        cop_base = 4.626 − 0.0417×m  if m ≤ 33
                 = 3.679 − 0.0130×m  if 33 < m ≤ 60
                 = 3.500 − 0.0100×m  if m > 60
        COP = cop_base × (39.5 / lift)

    Returns 0.0 when the heat pump is not running (m ≤ 1) or during a
    cold start where T_gen has not yet risen above the source temperature.
    Final result clamped to [COP_CLAMP_MIN, COP_CLAMP_MAX].
    """
    if modulation <= 1.0 or heat_gen_temp <= COP_SOURCE_TEMP:
        return 0.0

    lift = heat_gen_temp - COP_SOURCE_TEMP

    if heat_gen_temp <= COP_SH_MAX_TGEN:
        # ── Space heating regime ──────────────────────────────────────────
        if modulation < 12.0:
            cop_base = 0.5833 * modulation
        elif modulation <= 22.0:
            cop_base = 7.0
        else:
            cop_base = 7.988 - (0.0449 * modulation)
        cop = cop_base * (COP_SH_LIFT_REF / lift)
    else:
        # ── DHW regime ────────────────────────────────────────────────────
        if modulation <= 33.0:
            cop_base = 4.626 - (0.0417 * modulation)
        elif modulation <= 60.0:
            cop_base = 3.679 - (0.0130 * modulation)
        else:
            cop_base = 3.500 - (0.0100 * modulation)
        cop = cop_base * (COP_DHW_LIFT_REF / lift)

    return max(COP_CLAMP_MIN, min(COP_CLAMP_MAX, round(cop, 4)))


# ── Dispatcher signal builders ─────────────────────────────────────────────
def dp_signal(entry_id: str, dp_id: int) -> str:
    return f"{DOMAIN}_{entry_id}_dp_{dp_id}"

def heater_signal(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_electric_heater"

def cooling_signal(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_passive_cooling"

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

SENSOR_BY_DPID: dict[int, HovalSensorDescription] = {
    s.dp_id: s for s in SENSOR_DESCRIPTIONS
}

PERSISTENT_DPIDS: frozenset[int] = frozenset({23009})
