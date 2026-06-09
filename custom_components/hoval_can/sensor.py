"""Sensor platform for Hoval CAN."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.components.sensor import (
    SensorDeviceClass, SensorEntity, SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN, PERSISTENT_DPIDS, SENSOR_DESCRIPTIONS,
    DP_HEAT_GEN, DP_MODULATION, DP_THERMAL_POWER,
    connection_signal, dp_signal, heater_signal,
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
        HovalHeatPumpElecPowerSensor(coord, entry),
        HovalHeatPumpElecEnergySensor(coord, entry),
        HovalTotalElecPowerSensor(coord, entry),
        HovalTotalElecEnergySensor(coord, entry),
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
        self._on_since: datetime | None = None
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
        self._unsub = async_track_time_interval(
            self.hass, self._tick, _ENERGY_INTERVAL
        )
        self.async_on_remove(self._cleanup)

    def _cleanup(self) -> None:
        if self._unsub:
            self._unsub(); self._unsub = None
        if self._on_since is not None:
            self._flush(datetime.now(timezone.utc))

    @callback
    def _on_heater(self) -> None:
        is_on = self._coord.electric_heater_on
        if is_on is None:
            return
        now = datetime.now(timezone.utc)
        if is_on and self._on_since is None:
            self._on_since = now
        elif not is_on and self._on_since is not None:
            self._flush(now)
        self._attr_native_value = self._live(now)
        self.async_write_ha_state()

    @callback
    def _tick(self, now: datetime) -> None:
        if self._on_since is not None:
            self._attr_native_value = self._live(now)
            self.async_write_ha_state()

    def _flush(self, now: datetime) -> None:
        if self._on_since is not None:
            h = max(0.0, (now - self._on_since).total_seconds() / 3600.0)
            self._total_kwh += h * self._coord.heater_power_kw
            self._on_since = None

    def _live(self, now: datetime) -> float:
        extra = 0.0
        if self._on_since is not None:
            extra = (max(0.0, (now - self._on_since).total_seconds() / 3600.0)
                     * self._coord.heater_power_kw)
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
        self._last_ts: datetime | None = None
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

    @callback
    def _update(self) -> None:
        now     = datetime.now(timezone.utc)
        thermal = self._coord.get_value(DP_THERMAL_POWER)
        cop     = self._coord.cop

        # Integrate PREVIOUS interval using start-of-interval values
        if (self._last_thermal is not None and self._last_ts is not None
                and thermal is not None
                and self._last_cop > 0.0 and self._last_thermal > 0.0):
            h = max(0.0, (now - self._last_ts).total_seconds() / 3600.0)
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
            self._attr_native_value = round(hp_elec + heater_elec, 3)
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
        self._last_ts: datetime | None = None
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
        ):
            self.async_on_remove(
                async_dispatcher_connect(self.hass, sig, self._update)
            )
        self._unsub = async_track_time_interval(
            self.hass, self._tick, _ENERGY_INTERVAL
        )
        self.async_on_remove(self._cleanup)

    def _cleanup(self) -> None:
        if self._unsub:
            self._unsub(); self._unsub = None
        if self._last_elec_kw is not None and self._last_ts is not None:
            self._integrate(datetime.now(timezone.utc), None)

    @callback
    def _update(self) -> None:
        now       = datetime.now(timezone.utc)
        thermal   = self._coord.get_value(DP_THERMAL_POWER)
        heater_on = self._coord.electric_heater_on
        if thermal is None or heater_on is None:
            new_kw = None
        else:
            cop         = self._coord.cop
            hp_elec     = 0.0 if (cop == 0.0 or thermal == 0.0) else thermal / cop
            heater_elec = self._coord.heater_power_kw if heater_on else 0.0
            new_kw      = hp_elec + heater_elec
        self._integrate(now, new_kw)
        self._attr_native_value = round(self._total_kwh, 3)
        self.async_write_ha_state()

    @callback
    def _tick(self, now: datetime) -> None:
        if self._last_elec_kw is None or self._last_ts is None:
            return
        h = max(0.0, (now - self._last_ts).total_seconds() / 3600.0)
        displayed = round(self._total_kwh + h * self._last_elec_kw, 3)
        if displayed != self._attr_native_value:
            self._attr_native_value = displayed
            self.async_write_ha_state()

    def _integrate(self, now: datetime, new_kw) -> None:
        if (self._last_elec_kw is not None
                and self._last_ts is not None and new_kw is not None):
            h = max(0.0, (now - self._last_ts).total_seconds() / 3600.0)
            self._total_kwh += h * self._last_elec_kw
        if new_kw is not None:
            self._last_elec_kw = new_kw
            self._last_ts      = now
        else:
            self._last_elec_kw = None
            self._last_ts      = None
