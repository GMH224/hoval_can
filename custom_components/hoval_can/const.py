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
# NOTE: superseded as a Total Electrical Power/Energy input by the more
# granular CONF_BRINE_PUMP_POWER + CONF_HEATING_PUMP_POWER below (same
# physical pumps, modelled per-component instead of as one lump estimate).
# Kept configurable and still displayed on its own entity so existing history
# is not lost; see HovalPassiveCoolingPowerSensor.
CONF_COOLING_POWER       = "cooling_power_w"
DEFAULT_COOLING_POWER_W: float = 100.0  # W — typical circulation-pump draw
COOLING_POWER_MIN        = 0.0          # W
COOLING_POWER_MAX        = 500.0        # W

# Ground-source heat-source ("brine"/Sole) loop temperature. No CAN datapoint
# reports this — the Rücklauf/Vorlauf Erdsonde gauges on this installation are
# analog-only. Manual, seasonally-adjusted estimate; feeds the COP lift calc.
# Default raised 12.5 → 16.5 °C in v0.3.1: measured on the analog gauge during
# an active DHW charge (July 2026, 200 m borehole with summer passive-cooling
# recharge — expected winter value ~15 °C, so the annual swing is small).
CONF_SOURCE_TEMP             = "source_temp_c"
DEFAULT_SOURCE_TEMP_C: float = 16.5     # °C
SOURCE_TEMP_MIN              = -5.0     # °C
SOURCE_TEMP_MAX              = 25.0     # °C

# Heat-exchanger approach temperature ("k", v0.3.1). The compressor actually
# works between T_evap ≈ T_source − approach and T_cond ≈ T_gen + approach, so
# the EFFECTIVE lift never shrinks to zero even when the water-side lift does.
# Adding k to both the reference and the actual lift (see calculate_cop)
# preserves the calibrated anchor points exactly while removing the 1/lift
# divergence at small lifts — which with a 16.5 °C source and floor-heating
# flow temperatures would otherwise pin the COP at the 8.5 clamp for most of
# the heating season. Calibrate against the DpId 23009 hardware counter:
# each ±1 °C of k moves computed SH electricity by roughly ∓2-3 %.
CONF_APPROACH_K            = "cop_approach_k_c"
DEFAULT_APPROACH_K_C: float = 7.0   # °C — combined evaporator+condenser approach
APPROACH_K_MIN             = 0.0    # °C  (0 reproduces the pre-v0.3.1 formula)
APPROACH_K_MAX             = 15.0   # °C

# Ground-loop (brine/source) circulation pump. Per Hoval's own spec sheet this
# is "je eine drehzahlregulierte Hocheffizienzpumpe heizungs- bzw. soleseitig"
# — i.e. the SAME class of speed-regulated high-efficiency circulator as the
# heating-circuit pump, not a separate high-draw unit. Default set close to
# the heating pump's own default as a result; treat as an estimate until
# measured/confirmed against the actual brine-pump nameplate.
CONF_BRINE_PUMP_POWER             = "brine_pump_power_w"
DEFAULT_BRINE_PUMP_POWER_W: float = 30.0   # W
BRINE_PUMP_POWER_MIN              = 0.0    # W
BRINE_PUMP_POWER_MAX              = 200.0  # W

# Heating-circuit circulation pump. Nameplate (Hoval-branded, Imax 0.44 A,
# 4-40 W dynamic/electronic regulation range) — default is the median of that
# range.
CONF_HEATING_PUMP_POWER             = "heating_pump_power_w"
DEFAULT_HEATING_PUMP_POWER_W: float = 20.0  # W
HEATING_PUMP_POWER_MIN              = 0.0   # W
HEATING_PUMP_POWER_MAX              = 100.0 # W

# Baseline standby draw: TopTronic E controller electronics, idle sensors, and
# the Siemens GLB341.9E 3-way valve actuator (nameplate: 1.9 W / 5.8 VA).
# Always present whenever the unit is powered, independent of heat-pump/DHW/
# cooling state.
CONF_STANDBY_POWER             = "standby_power_w"
DEFAULT_STANDBY_POWER_W: float = 12.0  # W
STANDBY_POWER_MIN              = 0.0   # W
STANDBY_POWER_MAX              = 100.0 # W

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
DP_STATUS_HP       = 2053    # Heat Pump Status        U8
DP_HEAT_GEN        = 7       # Wärmeerzeuger-Ist     S16 dec=1 °C  ← COP input
DP_THERMAL_POWER   = 29051   # Current Heating Power U32 dec=1 kW
DP_MODULATION      = 20052   # Compressor Modulation U8  %         ← COP input
DP_FLOW_TEMP       = 2       # Vorlauf-Ist           S16 dec=1 °C  ← health T_sink
DP_WEZ_ELEC_TOTAL  = 23009   # Hardware lifetime electricity  U32 dec=3 MWh
DP_WEZ_CYCLES      = 2080    # WEZ Switch Cycles     U32 counter   ← health input
DP_HEATING_PROGRAM = 502     # Active Heating Program STR          ← health gate

DHW_STATUS_CHARGING = 8
HC_STATUS_PASSIVE_COOLING = 9   # status_heating_circuit value for passive cooling

# ── Dynamic COP formula ───────────────────────────────────────────────────
# Two-regime model using live temperature lift, with an approach-temperature
# ("k") correction and a blended regime transition (both v0.3.1).
#
# COP inputs:
#   m      = compressor modulation %     (DpId 20052 → sensor.hoval_can_compressor_modulation)
#   t      = heat generator temperature °C (DpId 7 → sensor.hoval_can_heat_generator_temperature)
#
# Regime selection: t ≤ 38 °C → Space Heating;  t ≥ 42 °C → DHW;
#                   38 < t < 42 °C → linear blend of both regimes (v0.3.1 —
#                   removes the ~16-19 % power step the hard 40 °C split
#                   produced mid-DHW-charge).
#
# Lift correction (v0.3.1): COP scales with (ref_lift + k) / (lift + k),
# where k models the combined evaporator + condenser heat-exchanger approach
# temperatures. Adding k to numerator AND denominator preserves the calibrated
# anchors exactly (lift = 17.5 °C SH, lift = 39.5 °C DHW) while saturating the
# curve at small lifts instead of diverging into the clamp:
#   Space heating reference lift : 17.5 °C  → t_gen reference = 30 °C @ src 12.5
#   DHW reference lift            : 39.5 °C  → t_gen reference = 52 °C @ src 12.5
# k = 0 reproduces the pre-v0.3.1 bare-lift formula bit-for-bit.
#
# Guard-rails:
#   m ≤ 1  or  t ≤ t_source  →  return 0.0  (heat pump off / cold start)
#   Final COP clamped to [COP_CLAMP_MIN, COP_CLAMP_MAX]

COP_SOURCE_TEMP: float  = DEFAULT_SOURCE_TEMP_C  # °C — fallback only; kept in
                                                 # lockstep with the option default
COP_SH_LIFT_REF: float  = 17.5   # °C — space heating reference lift
COP_DHW_LIFT_REF: float = 39.5   # °C — DHW reference lift
COP_SH_MAX_TGEN: float  = 40.0   # °C — nominal regime split (blend centre)
COP_BLEND_LOW_TGEN: float  = 38.0  # °C — pure Space Heating below this (v0.3.1)
COP_BLEND_HIGH_TGEN: float = 42.0  # °C — pure DHW above this (v0.3.1)
COP_CLAMP_MIN: float    = 1.0
COP_CLAMP_MAX: float    = 8.5


def _cop_base_sh(modulation: float) -> float:
    """Space-heating part-load efficiency curve (calibrated)."""
    if modulation < 12.0:
        return 0.5833 * modulation
    if modulation <= 22.0:
        return 7.0
    return 7.988 - (0.0449 * modulation)


def _cop_base_dhw(modulation: float) -> float:
    """DHW part-load efficiency curve (calibrated)."""
    if modulation <= 33.0:
        return 4.626 - (0.0417 * modulation)
    if modulation <= 60.0:
        return 3.679 - (0.0130 * modulation)
    return 3.500 - (0.0100 * modulation)


def calculate_cop(
    modulation: float,
    heat_gen_temp: float,
    source_temp: float = COP_SOURCE_TEMP,
    approach_k: float = DEFAULT_APPROACH_K_C,
) -> float:
    """Dynamic COP from compressor modulation and heat generator temperature.

    Two-regime piecewise model calibrated against real operating data,
    refined in v0.3.1 with an approach-temperature term and a blended
    regime transition:

    Space Heating (heat_gen_temp ≤ 38 °C):
        cop_base = 0.5833×m           if m < 12
                 = 7.0                if 12 ≤ m ≤ 22
                 = 7.988 − 0.0449×m  if m > 22
        COP = cop_base × (17.5 + k) / (lift + k)

    DHW (heat_gen_temp ≥ 42 °C):
        cop_base = 4.626 − 0.0417×m  if m ≤ 33
                 = 3.679 − 0.0130×m  if 33 < m ≤ 60
                 = 3.500 − 0.0100×m  if m > 60
        COP = cop_base × (39.5 + k) / (lift + k)

    38 °C < heat_gen_temp < 42 °C: linear blend of the two regime values
    (weight (t − 38)/4 toward DHW), removing the step discontinuity the
    hard 40 °C split produced in the electrical-power output mid-charge.

    ``approach_k`` (default DEFAULT_APPROACH_K_C = 7.0 °C, configurable via
    the CONF_APPROACH_K option) models the combined heat-exchanger approach
    temperatures: the refrigerant works between roughly T_source − approach
    and T_gen + approach, so the effective lift saturates instead of going
    to zero. Because k is added to the reference lift as well, the model is
    unchanged at its calibration anchors; k = 0 reproduces the pre-v0.3.1
    formula exactly.

    ``source_temp`` defaults to COP_SOURCE_TEMP but is normally passed in from
    the coordinator's configurable, seasonally-adjustable option (there is no
    CAN datapoint for ground-loop temperature on this installation).

    Returns 0.0 when the heat pump is not running (m ≤ 1) or during a
    cold start where T_gen has not yet risen above the source temperature.
    Final result clamped to [COP_CLAMP_MIN, COP_CLAMP_MAX].
    """
    if modulation <= 1.0 or heat_gen_temp <= source_temp:
        return 0.0

    k = max(0.0, approach_k)
    lift_eff = (heat_gen_temp - source_temp) + k
    cop_sh = _cop_base_sh(modulation) * ((COP_SH_LIFT_REF + k) / lift_eff)
    cop_dhw = _cop_base_dhw(modulation) * ((COP_DHW_LIFT_REF + k) / lift_eff)

    if heat_gen_temp <= COP_BLEND_LOW_TGEN:
        cop = cop_sh
    elif heat_gen_temp >= COP_BLEND_HIGH_TGEN:
        cop = cop_dhw
    else:
        w = ((heat_gen_temp - COP_BLEND_LOW_TGEN)
             / (COP_BLEND_HIGH_TGEN - COP_BLEND_LOW_TGEN))
        cop = (1.0 - w) * cop_sh + w * cop_dhw

    return max(COP_CLAMP_MIN, min(COP_CLAMP_MAX, round(cop, 4)))


# ── Health index (v0.3.2) ──────────────────────────────────────────────────
# Self-referential heat-pump health model. Two MEASURED daily features fused
# with Hotelling's T² against the unit's own rolling baseline:
#
#   CycleRate(day) = Δ DpId 2080 (WEZ Switch Cycles, hardware counter)
#   η(day)         = PF(day) / mean COP_carnot(day)          ("Gütegrad")
#     PF(day)      = ∫ DpId 29051 (thermal kW) dt  /  Δ DpId 23009 (kWh)
#     COP_carnot   = T_flow[K] / (T_flow − T_source)   per SH sample
#
# Design invariants (see AUDIT_v0.3.2 §"Model provenance"):
#   • ONLY measured CAN datapoints enter the model — the synthetic
#     calculate_cop() output is deliberately NOT an input (it contains no
#     measured electrical quantity, so η built on it would be circular).
#   • Mode gate uses real status datapoints: DHW via DpId 2052 == 8,
#     passive cooling via DpId 2051 == 9 (NOT the derived cooling-energy
#     estimate, which is a configured plug value).
#   • No fixed absolute thresholds — "elevated" is the baseline's own
#     empirical 95th percentile; "high" is the parametric Hotelling-T²
#     control limit (closed-form F quantile for p = 2). Hoval publishes no
#     field-level health thresholds for this unit; absolute numbers would
#     be fabricated.
#   • Δ DpId 23009 is quantised to 1 kWh — days below HEALTH_MIN_ELEC_KWH
#     are rejected rather than divided into noise.
HEALTH_SAMPLE_INTERVAL_S   = 300    # s between telemetry samples (5 min)
HEALTH_MAX_GAP_S           = 900    # s — longer sample gaps are not integrated
                                    # (restart / outage must not create energy)
HEALTH_MIN_COVERAGE_S      = 12 * 3600  # observed seconds for a day to count
HEALTH_MIN_SH_S            = 2 * 3600   # min SPACE_HEATING_ACTIVE time per day
HEALTH_PURITY_MAX          = 0.05   # (DHW+cooling)/observed — spec §4 "purity"
HEALTH_MIN_ELEC_KWH        = 5.0    # min daily Δ23009 vs 1 kWh quantisation
HEALTH_MIN_CARNOT_SAMPLES  = 6      # min valid SH samples for a daily Carnot mean
HEALTH_TSINK_XCHECK_MAX_C  = 3.0    # |flow − heat_gen| beyond this → suspect
HEALTH_MAX_SUSPECT_FRAC    = 0.5    # reject day if most SH samples suspect
HEALTH_ETA_PLAUSIBLE       = (0.08, 0.85)  # Gütegrad plausibility band.
# NOTE the floor is far below the literature's 0.4-0.6 "Gütegrad" range on
# purpose: literature values reference the refrigerant-side lift, while this
# η divides a WHOLE-UNIT daily PF (pumps + standby in Δ23009) by the pure
# WATER-side Carnot COP — which at this installation's small floor-heating
# lift (~13.5 K → COP_carnot ≈ 22) yields healthy values near 0.18. A 25 %
# efficiency loss must land INSIDE the band (≈ 0.13) so degradation is
# flagged by the T² model, not rejected as bad data; only pipeline-grade
# implausibility (η < 0.08, e.g. PF < 2 at this lift) is excluded.
# Calibrated by the end-to-end simulation in tests/test_health.py.
HEALTH_BASELINE_WINDOW     = 90     # qualifying days in the rolling baseline
HEALTH_BASELINE_MIN        = 30     # min qualifying days before an index exists
HEALTH_SIGMA_FLOOR         = 1e-6   # σ floor — degenerate baselines never div/0
HEALTH_RIDGE_EPS           = 0.01   # Σ + εI when near-singular (spec §8)
HEALTH_ELEVATED_PCTL       = 0.95   # empirical percentile → "elevated"
HEALTH_HIGH_F_Q            = 0.99   # F quantile → parametric "high" limit
HEALTH_ALERT_RUN_DAYS      = 5      # consecutive elevated days → sustained alert
HEALTH_STALE_MODE_DAYS     = 14     # no qualifying day in this many days
                                    # → insufficient_mode_data (e.g. summer)
HEALTH_HISTORY_MAX_DAYS    = 400    # stored day records (enables YoY anchor)
HEALTH_YOY_TOLERANCE_DAYS  = 21     # centre-match slack for the YoY window
HEALTH_STORE_SUFFIX        = "health"   # Store key suffix
# Heating-program strings that exclude SPACE_HEATING_ACTIVE (spec §3 —
# "Summer"/idle programs). Matched case-insensitively as substrings; an
# unseen program datapoint never blocks classification.
HEALTH_EXCLUDED_PROGRAMS   = ("sommer", "summer", "standby", "aus")

# Confidence-sensor component weights (data certainty, NOT health level).
# confidence = 100 × maturity × Σ(wᵢ · componentᵢ); see health.py.
HEALTH_CONF_W_RESOLUTION   = 0.30   # daily elec delta vs 1 kWh quantisation
HEALTH_CONF_W_YIELD        = 0.30   # qualifying-day yield over recent days
HEALTH_CONF_W_SENSOR       = 0.20   # 1 − sensor-suspect fraction
HEALTH_CONF_W_CONDITION    = 0.20   # Σ conditioning (|ρ| near 1 → T² unstable)
HEALTH_CONF_ELEC_FULL_KWH  = 20.0   # Δ23009 ≥ this → full resolution score
HEALTH_CONF_YIELD_WINDOW   = 14     # days over which yield is measured


# ── Dispatcher signal builders ─────────────────────────────────────────────
def dp_signal(entry_id: str, dp_id: int) -> str:
    return f"{DOMAIN}_{entry_id}_dp_{dp_id}"

def heater_signal(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_electric_heater"

def cooling_signal(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_passive_cooling"

def connection_signal(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_connection"

def health_signal(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_health"


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

# DpIds whose last-known value survives a Home Assistant restart.
#
# 23009 is the hardware lifetime-energy counter (entity-level restore only,
# pre-existing). The rest feed the derived power/COP calculations
# (HovalCANCoordinator.cop / electric_heater_on / passive_cooling_on /
# heat_pump_active); persisting them closes the gap where CAN only
# broadcasts a datapoint on change, so a value that hasn't changed since
# before the restart might otherwise not arrive again for a long time,
# leaving Total Electrical Power sitting at Unknown. Used both for each raw
# sensor's own entity-level restore (HovalPersistentSensor) and, in the
# coordinator, to seed internal state from HA's Store before any CAN data
# has arrived — see HovalCANCoordinator._async_load_persisted().
PERSISTENT_DPIDS: frozenset[int] = frozenset({
    DP_WEZ_ELEC_TOTAL,
    DP_WEZ_CYCLES,      # v0.3.2 — health sampler needs cycles right after restart
    DP_STATUS_HP, DP_STATUS_HC, DP_STATUS_WW,
    DP_MODULATION, DP_HEAT_GEN, DP_THERMAL_POWER,
    DP_DHW_ACTUAL, DP_DHW_SETPOINT,
})

# ── Coordinator-level persistent storage (HA Store helper) ─────────────────
STORAGE_VERSION = 1
# Debounce window for writing PERSISTENT_DPIDS to disk: CAN can broadcast a
# changing datapoint every ~2 s, so every update would otherwise hit flash
# storage constantly. Store.async_delay_save coalesces repeated calls within
# this window into a single write, and still flushes on HA shutdown.
PERSIST_SAVE_DELAY_S = 30
