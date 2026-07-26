"""Sensor platform for Hoval CAN."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass, SensorEntity, SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory, UnitOfEnergy,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN, PERSISTENT_DPIDS, SENSOR_DESCRIPTIONS,
    DP_HEAT_GEN, DP_MODULATION, DP_THERMAL_POWER,
    connection_signal, cooling_signal, dp_signal, health_signal,
    heater_signal,
)
from .coordinator import HovalCANCoordinator
from .health import HEALTH_STATUS_OPTIONS, STATUS_INSUFF_BASELINE

_LOGGER = logging.getLogger(__name__)
_ENERGY_INTERVAL = timedelta(seconds=60)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coord: HovalCANCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for desc in SENSOR_DESCRIPTIONS:
        cls = HovalPersistentSensor if desc.dp_id in PERSISTENT_DPIDS else HovalSensor
        entities.append(cls(coord, entry, desc))
    entities += [
        HovalDynamicCOPSensor(coord, entry),
        HovalElectricHeaterPowerSensor(coord, entry),
        HovalElectricHeaterEnergySensor(coord, entry),
        HovalPassiveCoolingPowerSensor(coord, entry),
        HovalPassiveCoolingEnergySensor(coord, entry),
        HovalHeatPumpElecPowerSensor(coord, entry),
        HovalHeatPumpElecEnergySensor(coord, entry),
        HovalBrinePumpPowerSensor(coord, entry),
        HovalHeatingPumpPowerSensor(coord, entry),
        HovalStandbyPowerSensor(coord, entry),
        HovalTotalElecPowerSensor(coord, entry),
        HovalTotalElecEnergySensor(coord, entry),
        # Health index (v0.3.2) — daily self-referential Hotelling-T² fusion
        # of measured cycling rate and measured Gütegrad, plus a data-
        # confidence companion. Tracker is created in __init__.py.
        HovalHealthIndexSensor(coord, entry),
        HovalHealthStatusSensor(coord, entry),
        HovalHealthConfidenceSensor(coord, entry),
        # Diagnostic telemetry (promoted from connectivity-sensor attributes
        # to first-class, recordable/alarmable entities).
        HovalDataAgeSensor(coord, entry),
        HovalReconnectsSensor(coord, entry),
        HovalFramingErrorsSensor(coord, entry),
        HovalDecodedCountSensor(coord, entry),
        HovalThroughputSensor(coord, entry),
        HovalFramingErrorRateSensor(coord, entry),
    ]
    async_add_entities(entities)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Hoval CAN",
        manufacturer="Hoval",
        model="WLAN Gateway",
        configuration_url=f"http://{entry.data['host']}",
    )


# ── Base ──────────────────────────────────────────────────────────────────

class HovalBaseEntity(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll     = False

    def __init__(self, coord: HovalCANCoordinator, entry: ConfigEntry) -> None:
        self._coord = coord
        self._entry = entry
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, connection_signal(self._entry.entry_id),
                lambda: self.async_write_ha_state(),
            )
        )

    @property
    def available(self) -> bool:
        return self._coord.connected


# ── Standard dpId sensor ───────────────────────────────────────────────────

class HovalSensor(HovalBaseEntity):
    """One entity per DatapointId. Unknown until first device broadcast."""

    def __init__(self, coord, entry, desc) -> None:
        super().__init__(coord, entry)
        self._desc = desc
        self._attr_unique_id                        = f"{entry.entry_id}_{desc.key}"
        self._attr_name                             = desc.name
        self._attr_native_unit_of_measurement       = desc.unit or None
        self._attr_device_class                     = desc.device_class
        self._attr_state_class                      = desc.state_class
        self._attr_icon                             = desc.icon
        self._attr_entity_category                  = desc.entity_category
        self._attr_entity_registry_enabled_default  = desc.enabled_default
        self._attr_native_value                     = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                dp_signal(self._entry.entry_id, self._desc.dp_id),
                self._update,
            )
        )

    @callback
    def _update(self) -> None:
        self._attr_native_value = self._coord.get_value(self._desc.dp_id)
        self.async_write_ha_state()


# ── Persistent dpId sensor ─────────────────────────────────────────────────

class HovalPersistentSensor(HovalSensor, RestoreEntity):
    """Restores last state across restarts (used for DpId=23009).
    Fresh device values always take precedence."""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            if last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE, None):
                try:
                    self._attr_native_value = float(last.state)
                    _LOGGER.debug("Hoval CAN: restored %s = %s",
                                  self._desc.key, self._attr_native_value)
                except (ValueError, TypeError):
                    pass


# ── Dynamic COP sensor ─────────────────────────────────────────────────────

class HovalDynamicCOPSensor(HovalBaseEntity):
    """Live COP calculated from compressor modulation and heat generator temperature.

    Two-regime piecewise model with approach-temperature correction and a
    blended transition (see const.calculate_cop, v0.3.1):
      • T_gen ≤ 38 °C → Space Heating regime
      • T_gen ≥ 42 °C → DHW regime
      • in between   → linear blend of both

    Shows 0.0 when heat pump is not running (modulation ≤ 1 or T_gen at/below
    the configured source temperature). Stays Unknown until both source
    DatapointIds have been received.
    """
    _attr_state_class                  = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement   = "COP"
    _attr_icon                         = "mdi:heating-coil"

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id = f"{entry.entry_id}_heat_pump_cop"
        self._attr_name      = "Heat Pump COP"
        self._attr_native_value = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        for dpid in (DP_MODULATION, DP_HEAT_GEN):
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    dp_signal(self._entry.entry_id, dpid),
                    self._update,
                )
            )

    @callback
    def _update(self) -> None:
        if (self._coord.get_value(DP_MODULATION) is None
                or self._coord.get_value(DP_HEAT_GEN) is None):
            return   # wait until both inputs are available
        self._attr_native_value = self._coord.cop
        self.async_write_ha_state()


# ── Electric heater: power ─────────────────────────────────────────────────

class HovalElectricHeaterPowerSensor(HovalBaseEntity):
    """Instantaneous power of the DHW electric heater.
    Returns heater_power_kw (configurable, default 3.0 kW) when active,
    0.0 when idle. Unknown until detection inputs are available."""
    _attr_device_class               = SensorDeviceClass.POWER
    _attr_state_class                = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kW"
    _attr_icon                       = "mdi:water-boiler"

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id    = f"{entry.entry_id}_electric_heater_power"
        self._attr_name         = "Electric Heater Power"
        self._attr_native_value = None
        self._has_value         = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, heater_signal(self._entry.entry_id), self._update,
            )
        )

    @callback
    def _update(self) -> None:
        state = self._coord.electric_heater_on
        if state is None and not self._has_value:
            return
        if state is not None:
            self._has_value = True
            self._attr_native_value = self._coord.heater_power_kw if state else 0.0
        self.async_write_ha_state()


# ── Electric heater: energy ────────────────────────────────────────────────

class HovalElectricHeaterEnergySensor(HovalBaseEntity, RestoreEntity):
    """Cumulative energy consumed by the electric DHW heater (kWh).

    Power = coordinator.heater_power_kw (configurable, default 3.0 kW).
    • Starts at 0.0 on first install; never returns to Unknown.
    • Restores total across HA restarts (RestoreEntity).
    • Only ever increases (TOTAL_INCREASING).
    • Holds value when heater state is Unknown; does not accumulate.
    • 3-decimal precision.
    """
    _attr_device_class               = SensorDeviceClass.ENERGY
    _attr_state_class                = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon                       = "mdi:lightning-bolt-circle"

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id    = f"{entry.entry_id}_electric_heater_energy"
        self._attr_name         = "Electric Heater Energy"
        self._total_kwh         = 0.0
        self._on_since: float | None = None
        self._unsub             = None
        self._attr_native_value = 0.0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            if last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE, None):
                try:
                    v = float(last.state)
                    if v >= 0.0:
                        self._total_kwh = v
                        self._attr_native_value = round(v, 3)
                        _LOGGER.debug("Hoval CAN: restored electric_heater_energy=%.3f kWh", v)
                except (ValueError, TypeError):
                    pass
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, heater_signal(self._entry.entry_id), self._on_heater,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, connection_signal(self._entry.entry_id),
                self._on_conn,
            )
        )
        self._unsub = async_track_time_interval(
            self.hass, self._tick, _ENERGY_INTERVAL
        )
        self.async_on_remove(self._cleanup)

    @callback
    def _on_conn(self) -> None:
        # Drop the open "heater on" interval on disconnect; otherwise a later
        # off-transition/cleanup would flush the entire downtime as energy.
        if not self._coord.connected:
            self._on_since = None

    def _cleanup(self) -> None:
        if self._unsub:
            self._unsub(); self._unsub = None
        if self._on_since is not None:
            self._flush(time.monotonic())

    @callback
    def _on_heater(self) -> None:
        is_on = self._coord.electric_heater_on
        if is_on is None:
            return
        now = time.monotonic()
        if is_on and self._on_since is None:
            self._on_since = now
        elif not is_on and self._on_since is not None:
            self._flush(now)
        self._attr_native_value = self._live(now)
        self.async_write_ha_state()

    @callback
    def _tick(self, now: datetime) -> None:
        # `now` (wall clock) from the HA timer is ignored; elapsed time is
        # measured with a monotonic clock for integrity across clock steps.
        if self._on_since is not None:
            self._attr_native_value = self._live(time.monotonic())
            self.async_write_ha_state()

    def _flush(self, now: float) -> None:
        if self._on_since is not None:
            h = max(0.0, (now - self._on_since) / 3600.0)
            self._total_kwh += h * self._coord.heater_power_kw
            self._on_since = None

    def _live(self, now: float) -> float:
        extra = 0.0
        if self._on_since is not None:
            extra = (max(0.0, (now - self._on_since) / 3600.0)
                     * self._coord.heater_power_kw)
        return round(self._total_kwh + extra, 3)


# ── Passive cooling: power ─────────────────────────────────────────────────

class HovalPassiveCoolingPowerSensor(HovalBaseEntity):
    """Instantaneous electrical power drawn during passive ("free") cooling.

    Returns cooling_power_kw (configurable, default 0.1 kW = 100 W) when the
    heating circuit reports passive-cooling mode (status 9), 0.0 otherwise.
    Unknown until the Heating Circuit Status datapoint is first received.

    NOTE: no longer part of Total Electrical Power/Energy — superseded by
    HovalBrinePumpPowerSensor + HovalHeatingPumpPowerSensor, which model the
    same physical pumps per-component (and share the same trigger with active
    heating/DHW, instead of a separate cooling-only estimate). Kept as its own
    entity purely so existing history on it isn't lost.
    """
    _attr_device_class               = SensorDeviceClass.POWER
    _attr_state_class                = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kW"
    _attr_icon                       = "mdi:snowflake"

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id    = f"{entry.entry_id}_passive_cooling_power"
        self._attr_name         = "Passive Cooling Power"
        self._attr_native_value = None
        self._has_value         = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, cooling_signal(self._entry.entry_id), self._update,
            )
        )

    @callback
    def _update(self) -> None:
        state = self._coord.passive_cooling_on
        if state is None and not self._has_value:
            return
        if state is not None:
            self._has_value = True
            self._attr_native_value = (
                self._coord.cooling_power_kw if state else 0.0
            )
        self.async_write_ha_state()


# ── Passive cooling: energy ────────────────────────────────────────────────

class HovalPassiveCoolingEnergySensor(HovalBaseEntity, RestoreEntity):
    """Cumulative electrical energy consumed during passive cooling (kWh).

    Power = coordinator.cooling_power_kw (configurable, default 0.1 kW).
    Mirrors the electric-heater energy sensor exactly: monotonic-clock
    integration, open interval discarded on disconnect, restores across
    restarts, only ever increases, 3-decimal precision.

    NOTE: no longer part of Total Electrical Energy — see
    HovalPassiveCoolingPowerSensor for why. Kept for its own history."""
    _attr_device_class               = SensorDeviceClass.ENERGY
    _attr_state_class                = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon                       = "mdi:snowflake-thermometer"

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id    = f"{entry.entry_id}_passive_cooling_energy"
        self._attr_name         = "Passive Cooling Energy"
        self._total_kwh         = 0.0
        self._on_since: float | None = None
        self._unsub             = None
        self._attr_native_value = 0.0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            if last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE, None):
                try:
                    v = float(last.state)
                    if v >= 0.0:
                        self._total_kwh = v
                        self._attr_native_value = round(v, 3)
                        _LOGGER.debug(
                            "Hoval CAN: restored passive_cooling_energy=%.3f kWh", v)
                except (ValueError, TypeError):
                    pass
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, cooling_signal(self._entry.entry_id), self._on_cooling,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, connection_signal(self._entry.entry_id),
                self._on_conn,
            )
        )
        self._unsub = async_track_time_interval(
            self.hass, self._tick, _ENERGY_INTERVAL
        )
        self.async_on_remove(self._cleanup)

    @callback
    def _on_conn(self) -> None:
        # Drop the open "cooling on" interval on disconnect; otherwise a later
        # off-transition/cleanup would flush the entire downtime as energy.
        if not self._coord.connected:
            self._on_since = None

    def _cleanup(self) -> None:
        if self._unsub:
            self._unsub(); self._unsub = None
        if self._on_since is not None:
            self._flush(time.monotonic())

    @callback
    def _on_cooling(self) -> None:
        is_on = self._coord.passive_cooling_on
        if is_on is None:
            return
        now = time.monotonic()
        if is_on and self._on_since is None:
            self._on_since = now
        elif not is_on and self._on_since is not None:
            self._flush(now)
        self._attr_native_value = self._live(now)
        self.async_write_ha_state()

    @callback
    def _tick(self, now: datetime) -> None:
        # `now` (wall clock) ignored; monotonic clock for integrity.
        if self._on_since is not None:
            self._attr_native_value = self._live(time.monotonic())
            self.async_write_ha_state()

    def _flush(self, now: float) -> None:
        if self._on_since is not None:
            h = max(0.0, (now - self._on_since) / 3600.0)
            self._total_kwh += h * self._coord.cooling_power_kw
            self._on_since = None

    def _live(self, now: float) -> float:
        extra = 0.0
        if self._on_since is not None:
            extra = (max(0.0, (now - self._on_since) / 3600.0)
                     * self._coord.cooling_power_kw)
        return round(self._total_kwh + extra, 3)


# ── Heat pump electrical: power ────────────────────────────────────────────

class HovalHeatPumpElecPowerSensor(HovalBaseEntity):
    """Instantaneous electrical power drawn by the heat pump compressor.

    Calculation: elec_kW = thermal_kW (DpId=29051) / COP(modulation, T_gen)

    Updates on thermal power, modulation, or T_gen changes.
    Returns 0.0 when COP=0 (heat pump not running).
    Unknown until DpId=29051 is first received. 3-decimal precision.
    """
    _attr_device_class               = SensorDeviceClass.POWER
    _attr_state_class                = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kW"
    _attr_icon                       = "mdi:heat-pump"

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id    = f"{entry.entry_id}_heat_pump_electrical_power"
        self._attr_name         = "Heat Pump Electrical Power"
        self._attr_native_value = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        for dpid in (DP_THERMAL_POWER, DP_MODULATION, DP_HEAT_GEN):
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    dp_signal(self._entry.entry_id, dpid),
                    self._update,
                )
            )

    @callback
    def _update(self) -> None:
        thermal = self._coord.get_value(DP_THERMAL_POWER)
        if thermal is None:
            self._attr_native_value = None
        else:
            cop = self._coord.cop
            self._attr_native_value = (
                0.0 if (cop == 0.0 or thermal == 0.0)
                else round(thermal / cop, 3)
            )
        self.async_write_ha_state()


# ── Heat pump electrical: energy ───────────────────────────────────────────

class HovalHeatPumpElecEnergySensor(HovalBaseEntity, RestoreEntity):
    """Cumulative electrical energy consumed by the heat pump compressor (kWh).

    Left Riemann sum, re-sampled on every update of DpId 29051 (thermal
    power), 20052 (modulation) and 7 (T_gen) — i.e. whenever ANY input to
    elec_kW = thermal / COP changes — plus a 60-second timer that commits the
    open interval at freshly recomputed values (v0.3.1). CAN broadcasts only
    on change, so without the extra subscriptions/tick a long constant-thermal
    plateau (e.g. a DHW charge) would be integrated at the COP frozen at the
    start of the interval; staleness is now capped at 60 s.
    Stores kW at start of each interval to integrate accurately.
    Resets tracking on a None thermal value to avoid gap accumulation; the
    timer never re-arms tracking while disconnected.
    • Starts at 0.0; restores across restarts; only increases; 3 decimals.
    """
    _attr_device_class               = SensorDeviceClass.ENERGY
    _attr_state_class                = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon                       = "mdi:heat-pump"

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id    = f"{entry.entry_id}_heat_pump_electrical_energy"
        self._attr_name         = "Heat Pump Electrical Energy"
        self._total_kwh         = 0.0
        self._last_thermal      = None
        self._last_cop          = 0.0
        self._last_ts: float | None = None
        self._attr_native_value = 0.0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            if last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE, None):
                try:
                    v = float(last.state)
                    if v >= 0.0:
                        self._total_kwh = v
                        self._attr_native_value = round(v, 3)
                        _LOGGER.debug("Hoval CAN: restored hp_elec_energy=%.3f kWh", v)
                except (ValueError, TypeError):
                    pass
        for dpid in (DP_THERMAL_POWER, DP_MODULATION, DP_HEAT_GEN):
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    dp_signal(self._entry.entry_id, dpid),
                    self._update,
                )
            )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, connection_signal(self._entry.entry_id),
                self._on_conn,
            )
        )
        # 60-s re-sample: commits the open interval at freshly recomputed
        # values so a long span without any input broadcast cannot freeze the
        # COP for more than a minute (v0.3.1).
        self.async_on_remove(
            async_track_time_interval(self.hass, self._tick, _ENERGY_INTERVAL)
        )

    @callback
    def _on_conn(self) -> None:
        # On disconnect, discard the open interval so the next sample after a
        # reconnect does not integrate the entire downtime as one lump.
        if not self._coord.connected:
            self._last_thermal = None
            self._last_cop     = 0.0
            self._last_ts      = None

    @callback
    def _update(self) -> None:
        # Signal path: always write state (an input just changed).
        self._sample(time.monotonic(), write_always=True)

    @callback
    def _tick(self, now: datetime) -> None:
        # Wall-clock `now` from the HA timer is ignored; elapsed time is
        # measured with a monotonic clock for integrity across clock steps.
        # Guards: never (re-)arm tracking. While disconnected, _on_conn
        # cleared tracking so downtime is not integrated — the timer must not
        # undo that. While tracking is cleared (pre-first-signal, post-None,
        # post-reconnect before a fresh 29051 broadcast), coordinator data
        # may be stale; only a dispatcher signal may arm the integrator —
        # identical to the pre-v0.3.1 semantics. The tick only CONTINUES an
        # interval a signal already opened.
        if not self._coord.connected:
            return
        if self._last_ts is None:
            return
        self._sample(time.monotonic(), write_always=False)

    def _sample(self, now: float, write_always: bool) -> None:
        """Integrate the previous interval and re-arm at current values.

        Left Riemann with start-of-interval values; monotonic clock — immune
        to NTP/DST wall-clock steps that would otherwise lose energy
        (backward step) or over-count (forward step).
        """
        thermal = self._coord.get_value(DP_THERMAL_POWER)
        cop     = self._coord.cop

        if (self._last_thermal is not None and self._last_ts is not None
                and thermal is not None
                and self._last_cop > 0.0 and self._last_thermal > 0.0):
            h = max(0.0, (now - self._last_ts) / 3600.0)
            self._total_kwh += h * (self._last_thermal / self._last_cop)

        if thermal is not None:
            self._last_thermal = thermal
            self._last_cop     = cop
            self._last_ts      = now
        else:
            self._last_thermal = None
            self._last_cop     = 0.0
            self._last_ts      = None

        displayed = round(self._total_kwh, 3)
        if write_always or displayed != self._attr_native_value:
            self._attr_native_value = displayed
            self.async_write_ha_state()


# ── Brine/source pump: power ────────────────────────────────────────────────

class HovalBrinePumpPowerSensor(HovalBaseEntity):
    """Instantaneous power of the ground-loop (brine/source) circulation pump.

    Returns brine_pump_power_w (configurable, default 30 W) whenever
    coordinator.pumps_active is True — i.e. the compressor is active
    (heating/DHW) or the heating circuit is in passive/free cooling, since
    both draw the ground loop. 0.0 when known-inactive; Unknown until enough
    data has been seen to tell either way."""
    _attr_device_class               = SensorDeviceClass.POWER
    _attr_state_class                = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kW"
    _attr_icon                       = "mdi:pump"

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id    = f"{entry.entry_id}_brine_pump_power"
        self._attr_name         = "Brine Pump Power"
        self._attr_native_value = None
        self._has_value         = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        for sig in (
            dp_signal(self._entry.entry_id, DP_MODULATION),
            cooling_signal(self._entry.entry_id),
        ):
            self.async_on_remove(
                async_dispatcher_connect(self.hass, sig, self._update)
            )

    @callback
    def _update(self) -> None:
        active = self._coord.pumps_active
        if active is None and not self._has_value:
            return
        if active is not None:
            self._has_value = True
            self._attr_native_value = (
                round(self._coord.brine_pump_kw, 3) if active else 0.0
            )
        self.async_write_ha_state()


# ── Heating-circuit pump: power ─────────────────────────────────────────────

class HovalHeatingPumpPowerSensor(HovalBaseEntity):
    """Instantaneous power of the heating-circuit circulation pump.

    Same trigger as the brine pump (coordinator.pumps_active): the pump runs
    during active heating/DHW and during passive cooling. Returns
    heating_pump_power_w (configurable, default 20 W — the median of the
    pump's own 4-40 W nameplate range)."""
    _attr_device_class               = SensorDeviceClass.POWER
    _attr_state_class                = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kW"
    _attr_icon                       = "mdi:pump"

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id    = f"{entry.entry_id}_heating_pump_power"
        self._attr_name         = "Heating Pump Power"
        self._attr_native_value = None
        self._has_value         = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        for sig in (
            dp_signal(self._entry.entry_id, DP_MODULATION),
            cooling_signal(self._entry.entry_id),
        ):
            self.async_on_remove(
                async_dispatcher_connect(self.hass, sig, self._update)
            )

    @callback
    def _update(self) -> None:
        active = self._coord.pumps_active
        if active is None and not self._has_value:
            return
        if active is not None:
            self._has_value = True
            self._attr_native_value = (
                round(self._coord.heating_pump_kw, 3) if active else 0.0
            )
        self.async_write_ha_state()


# ── Standby: power ──────────────────────────────────────────────────────────

class HovalStandbyPowerSensor(HovalBaseEntity):
    """Baseline standby power (TopTronic E controller electronics + Siemens
    GLB341.9E valve actuator idle draw). Configurable (standby_power_w,
    default 12 W). Always present whenever the unit is powered — independent
    of heat-pump/DHW/cooling state, so this never reports Unknown while the
    gateway connection is up."""
    _attr_device_class               = SensorDeviceClass.POWER
    _attr_state_class                = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kW"
    _attr_icon                       = "mdi:power-standby"

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id    = f"{entry.entry_id}_standby_power"
        self._attr_name         = "Standby Power"
        self._attr_native_value = round(coord.standby_kw, 3)


# ── Total electrical: power ────────────────────────────────────────────────

class HovalTotalElecPowerSensor(HovalBaseEntity):
    """Total instantaneous electrical power: heat pump (compressor, via
    measured thermal power / dynamic COP) + electric heater + brine pump +
    heating-circuit pump + standby. Unknown inputs are treated as 0 (see
    `_update`), so the total reports known loads even before every datapoint
    has been broadcast; a dead/stalled link is surfaced via `available`.
    Standby is unconditional, so this is never 0 due to "nothing running" —
    only Unknown/Unavailable can do that. 3-decimal precision."""
    _attr_device_class               = SensorDeviceClass.POWER
    _attr_state_class                = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kW"
    _attr_icon                       = "mdi:lightning-bolt"

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id    = f"{entry.entry_id}_total_electrical_power"
        self._attr_name         = "Total Electrical Power"
        self._attr_native_value = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        for sig in (
            dp_signal(self._entry.entry_id, DP_THERMAL_POWER),
            dp_signal(self._entry.entry_id, DP_MODULATION),
            dp_signal(self._entry.entry_id, DP_HEAT_GEN),
            heater_signal(self._entry.entry_id),
            cooling_signal(self._entry.entry_id),
        ):
            self.async_on_remove(
                async_dispatcher_connect(self.hass, sig, self._update)
            )

    @callback
    def _update(self) -> None:
        # Zero-fill unknown inputs rather than blanking the whole total. A dead
        # or stalled gateway is already surfaced via `available` (coordinator
        # `connected` + data watchdog), so an input that is None while the
        # entity is available means only that its datapoint has not been
        # broadcast yet (e.g. status_dhw stays dormant through long passive-
        # cooling spells on CAN). Treating that as 0 lets the total still report
        # known loads such as passive cooling instead of reading "unknown".
        thermal   = self._coord.get_value(DP_THERMAL_POWER)
        heater_on = self._coord.electric_heater_on
        cop       = self._coord.cop
        hp_elec   = (0.0 if (thermal is None or thermal == 0.0 or cop == 0.0)
                     else thermal / cop)
        heater_elec = self._coord.heater_power_kw if heater_on else 0.0
        pumps_on    = bool(self._coord.pumps_active)  # None -> False (zero-fill)
        brine_elec   = self._coord.brine_pump_kw if pumps_on else 0.0
        heating_elec = self._coord.heating_pump_kw if pumps_on else 0.0
        standby_elec = self._coord.standby_kw  # always on, never zero-filled
        self._attr_native_value = round(
            hp_elec + heater_elec + brine_elec + heating_elec + standby_elec, 3
        )
        self.async_write_ha_state()


# ── Total electrical: energy ───────────────────────────────────────────────

class HovalTotalElecEnergySensor(HovalBaseEntity, RestoreEntity):
    """Total cumulative electrical energy: heat pump (compressor) + electric
    heater + brine pump + heating-circuit pump + standby (kWh).

    Independent persistent counter — not a runtime sum of sub-sensors.
    Integrates on every update of DpId 29051 (thermal power), 20052
    (modulation) and 7 (T_gen) — the full COP input set, matching Total
    Electrical Power's subscriptions (v0.3.1) — plus heater and cooling
    state changes. The 60-second timer now COMMITS the open interval at a
    freshly recomputed kW instead of only refreshing the display (v0.3.1):
    CAN broadcasts only on change, so this caps any COP/state staleness in
    the integral at one minute. Unknown inputs are treated as 0 (see Total
    Electrical Power), so pump energy keeps integrating through
    passive-cooling spells even before status_dhw has been broadcast;
    standby is never zero-filled. The integrator only pauses on disconnect
    (the timer never re-arms tracking while disconnected).
    • Starts at 0.0; restores across restarts; only increases; 3 decimals.
    """
    _attr_device_class               = SensorDeviceClass.ENERGY
    _attr_state_class                = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon                       = "mdi:transmission-tower"

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id    = f"{entry.entry_id}_total_electrical_energy"
        self._attr_name         = "Total Electrical Energy"
        self._total_kwh         = 0.0
        self._last_elec_kw      = None
        self._last_ts: float | None = None
        self._unsub             = None
        self._attr_native_value = 0.0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            if last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE, None):
                try:
                    v = float(last.state)
                    if v >= 0.0:
                        self._total_kwh = v
                        self._attr_native_value = round(v, 3)
                        _LOGGER.debug("Hoval CAN: restored total_elec_energy=%.3f kWh", v)
                except (ValueError, TypeError):
                    pass
        for sig in (
            dp_signal(self._entry.entry_id, DP_THERMAL_POWER),
            dp_signal(self._entry.entry_id, DP_MODULATION),
            dp_signal(self._entry.entry_id, DP_HEAT_GEN),
            heater_signal(self._entry.entry_id),
            cooling_signal(self._entry.entry_id),
        ):
            self.async_on_remove(
                async_dispatcher_connect(self.hass, sig, self._update)
            )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, connection_signal(self._entry.entry_id),
                self._on_conn,
            )
        )
        self._unsub = async_track_time_interval(
            self.hass, self._tick, _ENERGY_INTERVAL
        )
        self.async_on_remove(self._cleanup)

    @callback
    def _on_conn(self) -> None:
        # Discard the open interval on disconnect so a reconnect cannot
        # integrate the downtime as a single fictitious lump.
        if not self._coord.connected:
            self._last_elec_kw = None
            self._last_ts      = None

    def _cleanup(self) -> None:
        if self._unsub:
            self._unsub(); self._unsub = None
        if self._last_elec_kw is not None and self._last_ts is not None:
            self._integrate(time.monotonic(), None)

    def _current_kw(self) -> float:
        """Recompute the total electrical draw from live coordinator state.

        Zero-fill unknown inputs (see Total Electrical Power). Stays a number
        whenever the entity is available, so pump energy keeps integrating
        through passive-cooling spells when status_dhw is dormant.
        """
        thermal   = self._coord.get_value(DP_THERMAL_POWER)
        heater_on = self._coord.electric_heater_on
        cop       = self._coord.cop
        hp_elec   = (0.0 if (thermal is None or thermal == 0.0 or cop == 0.0)
                     else thermal / cop)
        heater_elec = self._coord.heater_power_kw if heater_on else 0.0
        pumps_on    = bool(self._coord.pumps_active)
        brine_elec   = self._coord.brine_pump_kw if pumps_on else 0.0
        heating_elec = self._coord.heating_pump_kw if pumps_on else 0.0
        standby_elec = self._coord.standby_kw
        return hp_elec + heater_elec + brine_elec + heating_elec + standby_elec

    @callback
    def _update(self) -> None:
        self._integrate(time.monotonic(), self._current_kw())
        self._attr_native_value = round(self._total_kwh, 3)
        self.async_write_ha_state()

    @callback
    def _tick(self, now: datetime) -> None:
        # Wall-clock `now` ignored; monotonic clock used for elapsed time.
        # v0.3.1: the tick COMMITS the open interval at a freshly recomputed
        # kW (previously it only refreshed the display at the stale rate) —
        # capping COP/state staleness in the integral at one minute. Guarded
        # on `connected` so the timer never re-arms tracking that _on_conn
        # cleared, i.e. downtime is still never integrated.
        if not self._coord.connected:
            return
        if self._last_elec_kw is None or self._last_ts is None:
            return
        self._integrate(time.monotonic(), self._current_kw())
        displayed = round(self._total_kwh, 3)
        if displayed != self._attr_native_value:
            self._attr_native_value = displayed
            self.async_write_ha_state()

    def _integrate(self, now: float, new_kw) -> None:
        if (self._last_elec_kw is not None
                and self._last_ts is not None and new_kw is not None):
            h = max(0.0, (now - self._last_ts) / 3600.0)
            self._total_kwh += h * self._last_elec_kw
        if new_kw is not None:
            self._last_elec_kw = new_kw
            self._last_ts      = now
        else:
            self._last_elec_kw = None
            self._last_ts      = None


# ── Diagnostic telemetry ───────────────────────────────────────────────────

# ── Health index (v0.3.2) ──────────────────────────────────────────────────

class HovalHealthBaseSensor(SensorEntity):
    """Base for the health entities.

    State is derived from the HealthTracker's stored daily statistics, not
    from the live TCP stream — so these stay available (and meaningful)
    while the gateway is disconnected, like the diagnostic sensors. Updates
    are pushed via health_signal after every processed 5-minute sample.
    The tracker is attached to the coordinator by __init__.py before the
    sensor platform is forwarded, so it is always present here.
    """
    _attr_has_entity_name = True
    _attr_should_poll     = False

    def __init__(self, coord: HovalCANCoordinator, entry: ConfigEntry) -> None:
        self._coord = coord
        self._entry = entry
        self._attr_device_info = _device_info(entry)

    @property
    def available(self) -> bool:
        return getattr(self._coord, "health_tracker", None) is not None

    @property
    def _model(self):
        tracker = getattr(self._coord, "health_tracker", None)
        return tracker.model if tracker is not None else None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, health_signal(self._entry.entry_id),
                self._update,
            )
        )
        self._update()

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()


class HovalHealthIndexSensor(HovalHealthBaseSensor):
    """Hotelling-T² health index for the latest qualifying day.

    Unbounded and meaningful ONLY relative to this unit's own history —
    no absolute 0-100 score exists for this installation (spec §11).
    Unknown until ≥ 30 qualifying SPACE_HEATING_ACTIVE days are baselined;
    all model internals are exposed as attributes for dashboards/automation.
    """
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon        = "mdi:heart-pulse"
    _attr_suggested_display_precision = 2

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id = f"{entry.entry_id}_health_index"
        self._attr_name      = "Health Index T2"

    @property
    def native_value(self):
        model = self._model
        if model is None:
            return None
        return model.latest.get("t2")

    @property
    def extra_state_attributes(self):
        model = self._model
        if model is None:
            return None
        latest = model.latest
        return {
            "status": latest.get("status"),
            "z_cycle": latest.get("z_cycle"),
            "z_eta": latest.get("z_eta"),
            "cycle_rate_per_day": latest.get("cycle_rate"),
            "guetegrad_eta": latest.get("eta"),
            "daily_performance_factor": latest.get("pf"),
            "daily_carnot_cop": latest.get("carnot"),
            "baseline_days": latest.get("baseline_n"),
            "baseline_mu_cycle": latest.get("mu_cycle"),
            "baseline_mu_eta": latest.get("mu_eta"),
            "correlation_rho": latest.get("rho"),
            "ridge_regularized": latest.get("ridged"),
            "elevated_limit_p95": latest.get("elevated_limit"),
            "high_limit_f99": latest.get("high_limit"),
            "consecutive_elevated_days": latest.get("consecutive_elevated"),
            "sustained_alert": latest.get("sustained_alert"),
            "eta_yoy_delta": latest.get("eta_yoy_delta"),
            "last_qualifying_day": latest.get("last_qualifying_day"),
        }


class HovalHealthStatusSensor(HovalHealthBaseSensor):
    """Categorical health status (spec §11 flag set) as an ENUM entity —
    normal / elevated / high / insufficient_baseline /
    insufficient_mode_data. Automation-friendly companion to the T² value."""
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options      = list(HEALTH_STATUS_OPTIONS)
    _attr_icon         = "mdi:stethoscope"

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id = f"{entry.entry_id}_health_status"
        self._attr_name      = "Health Status"

    @property
    def native_value(self):
        model = self._model
        if model is None:
            return None
        return model.latest.get("status") or STATUS_INSUFF_BASELINE

    @property
    def extra_state_attributes(self):
        model = self._model
        if model is None:
            return None
        last = model.history[-1] if model.history else None
        return {
            "last_closed_day": last.get("day") if last else None,
            "last_day_qualifying": last.get("qualifying") if last else None,
            "last_day_reject_reasons": last.get("reject_reasons") if last else None,
            "last_day_unobserved_h": last.get("unknown_h") if last else None,
            "history_days": len(model.history),
        }


class HovalHealthConfidenceSensor(HovalHealthBaseSensor):
    """Certainty of the health-index DATA (0-100 %) — explicitly NOT a
    health level. A structurally sound heat pump with a noisy, sparse, or
    immature data pipeline reads LOW here while Health Status may still
    read "normal"; treat T² excursions at low confidence as unproven.
    Component scores are exposed as attributes (see health.py::confidence).
    """
    _attr_state_class                = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "%"
    _attr_icon                       = "mdi:shield-check"
    _attr_suggested_display_precision = 0

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id = f"{entry.entry_id}_health_confidence"
        self._attr_name      = "Health Confidence"

    @property
    def native_value(self):
        model = self._model
        if model is None:
            return None
        return model.confidence().get("confidence")

    @property
    def extra_state_attributes(self):
        model = self._model
        if model is None:
            return None
        conf = model.confidence()
        conf.pop("confidence", None)
        return conf


class HovalDiagnosticSensor(SensorEntity):
    """Base for polled diagnostic entities.

    Promotes coordinator health counters (previously only attributes on the
    connectivity binary_sensor) to first-class sensor states so they are
    recorded to long-term statistics, graphable, alarmable, and visible to
    state-based exporters (InfluxDB / Prometheus / MQTT). Polled rather than
    pushed because values such as data-age change continuously and the
    framing/decoded counters change inside the parser without a signal.

    Stays available even while disconnected — surfacing health is the point.
    """
    _attr_has_entity_name = True
    _attr_should_poll     = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: HovalCANCoordinator, entry: ConfigEntry) -> None:
        self._coord = coord
        self._entry = entry
        self._attr_device_info = _device_info(entry)

    @property
    def available(self) -> bool:
        return True


class HovalDataAgeSensor(HovalDiagnosticSensor):
    """Seconds since the last decoded datapoint (staleness)."""
    _attr_device_class               = SensorDeviceClass.DURATION
    _attr_state_class                = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "s"
    _attr_icon                       = "mdi:timer-sand"

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id = f"{entry.entry_id}_diag_data_age"
        self._attr_name      = "Gateway Data Age"

    @property
    def native_value(self):
        age = self._coord.last_data_age
        return None if age is None else round(age, 1)


class HovalReconnectsSensor(HovalDiagnosticSensor):
    """Cumulative successful reconnects since the integration loaded."""
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon        = "mdi:lan-pending"

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id = f"{entry.entry_id}_diag_reconnects"
        self._attr_name      = "Gateway Reconnects"

    @property
    def native_value(self):
        return self._coord.reconnect_count


class HovalFramingErrorsSensor(HovalDiagnosticSensor):
    """Cumulative frame desync events since the integration loaded."""
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon        = "mdi:alert-circle-outline"

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id = f"{entry.entry_id}_diag_framing_errors"
        self._attr_name      = "Gateway Framing Errors"

    @property
    def native_value(self):
        return self._coord.framing_errors


class HovalDecodedCountSensor(HovalDiagnosticSensor):
    """Cumulative decoded datapoints since load (derive rate for throughput)."""
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon        = "mdi:counter"

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id = f"{entry.entry_id}_diag_decoded_count"
        self._attr_name      = "Gateway Datapoints Decoded"

    @property
    def native_value(self):
        return self._coord.decoded_count


class HovalThroughputSensor(HovalDiagnosticSensor):
    """Decoded datapoints per minute (sliding 60-min window).

    The 'is data flowing' signal: drops toward 0 when the stream stalls, so it
    pairs with Data Age to distinguish a quiet link from a dead one. Unknown
    during the warm-up window after (re)load."""
    _attr_state_class                = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "datapoints/min"
    _attr_icon                       = "mdi:speedometer"

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id = f"{entry.entry_id}_diag_throughput"
        self._attr_name      = "Gateway Throughput"

    @property
    def native_value(self):
        rate = self._coord.decoded_rate_per_min
        return None if rate is None else round(rate, 1)


class HovalFramingErrorRateSensor(HovalDiagnosticSensor):
    """Framing errors per hour (sliding 15-min window).

    The 'is the stream clean' signal — restart-robust and directly alertable
    (e.g. notify if > N/h). Because the device streams at a near-constant
    cadence, this tracks parser health without the denominator instability of
    an errors-per-decoded ratio. Unknown during the warm-up window."""
    _attr_state_class                = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "errors/h"
    _attr_icon                       = "mdi:alert-decagram-outline"

    def __init__(self, coord, entry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id = f"{entry.entry_id}_diag_framing_error_rate"
        self._attr_name      = "Gateway Framing Error Rate"

    @property
    def native_value(self):
        rate = self._coord.framing_error_rate_per_h
        return None if rate is None else round(rate, 2)
