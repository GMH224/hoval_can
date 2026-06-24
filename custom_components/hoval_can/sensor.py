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
    connection_signal, cooling_signal, dp_signal, heater_signal,
)
from .coordinator import HovalCANCoordinator

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
        HovalTotalElecPowerSensor(coord, entry),
        HovalTotalElecEnergySensor(coord, entry),
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

    Two-regime piecewise model (see const.calculate_cop):
      • T_gen ≤ 40 °C → Space Heating regime
      • T_gen >  40 °C → DHW regime

    Shows 0.0 when heat pump is not running (modulation ≤ 1 or T_gen ≤ 12.5 °C).
    Stays Unknown until both source DatapointIds have been received.
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
    Unknown until the Heating Circuit Status datapoint is first received."""
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
    restarts, only ever increases, 3-decimal precision."""
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

    Left Riemann sum on every DpId=29051 update (~2 s intervals).
    Stores COP at start of each interval to integrate accurately.
    Resets tracking on None signal to avoid gap accumulation.
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
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                dp_signal(self._entry.entry_id, DP_THERMAL_POWER),
                self._update,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, connection_signal(self._entry.entry_id),
                self._on_conn,
            )
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
        now     = time.monotonic()
        thermal = self._coord.get_value(DP_THERMAL_POWER)
        cop     = self._coord.cop

        # Integrate PREVIOUS interval using start-of-interval values.
        # Monotonic clock: immune to NTP/DST wall-clock steps that would
        # otherwise lose energy (backward step) or over-count (forward step).
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

        self._attr_native_value = round(self._total_kwh, 3)
        self.async_write_ha_state()


# ── Total electrical: power ────────────────────────────────────────────────

class HovalTotalElecPowerSensor(HovalBaseEntity):
    """Total instantaneous electrical power: heat pump + electric heater.
    Unknown until both sources are available. 3-decimal precision."""
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
        thermal   = self._coord.get_value(DP_THERMAL_POWER)
        heater_on = self._coord.electric_heater_on
        if thermal is None or heater_on is None:
            self._attr_native_value = None
        else:
            cop         = self._coord.cop
            hp_elec     = 0.0 if (cop == 0.0 or thermal == 0.0) else thermal / cop
            heater_elec = self._coord.heater_power_kw if heater_on else 0.0
            # Passive cooling: only add when actively cooling. Unknown/None is
            # treated as 0 so installs without a cooling circuit never regress.
            cooling_elec = (self._coord.cooling_power_kw
                            if self._coord.passive_cooling_on else 0.0)
            self._attr_native_value = round(
                hp_elec + heater_elec + cooling_elec, 3)
        self.async_write_ha_state()


# ── Total electrical: energy ───────────────────────────────────────────────

class HovalTotalElecEnergySensor(HovalBaseEntity, RestoreEntity):
    """Total cumulative electrical energy: heat pump + electric heater (kWh).

    Independent persistent counter — not a runtime sum of sub-sensors.
    Integrates on every DpId=29051 update and heater state change.
    60-second timer keeps value current between infrequent heater events.
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

    @callback
    def _update(self) -> None:
        now       = time.monotonic()
        thermal   = self._coord.get_value(DP_THERMAL_POWER)
        heater_on = self._coord.electric_heater_on
        if thermal is None or heater_on is None:
            new_kw = None
        else:
            cop         = self._coord.cop
            hp_elec     = 0.0 if (cop == 0.0 or thermal == 0.0) else thermal / cop
            heater_elec = self._coord.heater_power_kw if heater_on else 0.0
            # Passive cooling: only add when actively cooling. Unknown/None is
            # treated as 0 so installs without a cooling circuit never regress.
            cooling_elec = (self._coord.cooling_power_kw
                            if self._coord.passive_cooling_on else 0.0)
            new_kw      = hp_elec + heater_elec + cooling_elec
        self._integrate(now, new_kw)
        self._attr_native_value = round(self._total_kwh, 3)
        self.async_write_ha_state()

    @callback
    def _tick(self, now: datetime) -> None:
        # Wall-clock `now` ignored; monotonic clock used for elapsed time.
        if self._last_elec_kw is None or self._last_ts is None:
            return
        h = max(0.0, (time.monotonic() - self._last_ts) / 3600.0)
        displayed = round(self._total_kwh + h * self._last_elec_kw, 3)
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
