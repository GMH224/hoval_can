"""Sensor platform for Hoval CAN."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
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
    DOMAIN,
    HEATER_RATED_POWER_KW,
    PERSISTENT_DPIDS,
    SENSOR_DESCRIPTIONS,
    DP_THERMAL_POWER,
    connection_signal,
    dp_signal,
    heater_signal,
)
from .coordinator import HovalCANCoordinator

_LOGGER = logging.getLogger(__name__)

# Interval for live-updating displayed energy totals while sources are active
_ENERGY_UPDATE_INTERVAL = timedelta(seconds=60)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HovalCANCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []

    for desc in SENSOR_DESCRIPTIONS:
        if desc.dp_id in PERSISTENT_DPIDS:
            entities.append(HovalPersistentSensor(coordinator, entry, desc))
        else:
            entities.append(HovalSensor(coordinator, entry, desc))

    # Derived electric-heater sensors
    entities.append(HovalElectricHeaterPowerSensor(coordinator, entry))
    entities.append(HovalElectricHeaterEnergySensor(coordinator, entry))

    # COP-based electrical sensors (new in v0.2)
    entities.append(HovalHeatPumpElecPowerSensor(coordinator, entry))
    entities.append(HovalHeatPumpElecEnergySensor(coordinator, entry))
    entities.append(HovalTotalElecPowerSensor(coordinator, entry))
    entities.append(HovalTotalElecEnergySensor(coordinator, entry))

    async_add_entities(entities)


# ── Shared device info ─────────────────────────────────────────────────────

def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Hoval CAN",
        manufacturer="Hoval",
        model="WLAN Gateway",
        configuration_url=f"http://{entry.data['host']}",
    )


# ── Base entity ────────────────────────────────────────────────────────────

class HovalBaseEntity(SensorEntity):
    """Common base: no polling, available only when connected."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, coordinator: HovalCANCoordinator, entry: ConfigEntry
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                connection_signal(self._entry.entry_id),
                self._handle_connection_change,
            )
        )

    @callback
    def _handle_connection_change(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._coordinator.connected


# ── Standard dpId sensor ───────────────────────────────────────────────────

class HovalSensor(HovalBaseEntity):
    """One sensor entity per DatapointId. Starts Unknown; updates on first
    broadcast from the device."""

    def __init__(self, coordinator, entry, desc) -> None:
        super().__init__(coordinator, entry)
        self._desc = desc
        self._attr_unique_id = f"{entry.entry_id}_{desc.key}"
        self._attr_name = desc.name
        self._attr_native_unit_of_measurement = desc.unit or None
        self._attr_device_class = desc.device_class
        self._attr_state_class = desc.state_class
        self._attr_icon = desc.icon
        self._attr_entity_category = desc.entity_category
        self._attr_entity_registry_enabled_default = desc.enabled_default
        self._attr_native_value = None   # Unknown until first reading

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                dp_signal(self._entry.entry_id, self._desc.dp_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self._attr_native_value = self._coordinator.get_value(self._desc.dp_id)
        self.async_write_ha_state()


# ── Persistent dpId sensor ─────────────────────────────────────────────────

class HovalPersistentSensor(HovalSensor, RestoreEntity):
    """Like HovalSensor but restores last known value across HA restarts.

    Used for hardware energy counters such as Total WEZ Electrical Energy
    (DpId=23009). The device holds the true cumulative value; restore just
    covers the short window between HA startup and the device's first
    broadcast (~60 s). Fresh device values always take precedence.
    """

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            if last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE, None):
                try:
                    self._attr_native_value = float(last.state)
                    _LOGGER.debug(
                        "Hoval CAN: restored %s = %s",
                        self._desc.key, self._attr_native_value,
                    )
                except (ValueError, TypeError):
                    pass


# ── Electric heater: power sensor ─────────────────────────────────────────

class HovalElectricHeaterPowerSensor(HovalBaseEntity):
    """Instantaneous power of the electric DHW heater.

    Returns HEATER_RATED_POWER_KW (3.0 kW) when active, 0.0 when idle.
    Stays Unknown until all four source DatapointIds have been received.
    Once a value has been set it never returns to Unknown.
    """

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kW"
    _attr_icon = "mdi:water-boiler"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_electric_heater_power"
        self._attr_name = "Electric Heater Power"
        self._attr_native_value = None
        self._has_value = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                heater_signal(self._entry.entry_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        state = self._coordinator.electric_heater_on
        if state is None and not self._has_value:
            return   # not enough data yet — stay Unknown
        if state is not None:
            self._has_value = True
            self._attr_native_value = HEATER_RATED_POWER_KW if state else 0.0
        # if state is None but _has_value is True: hold last known value
        self.async_write_ha_state()


# ── Electric heater: cumulative energy sensor ──────────────────────────────

class HovalElectricHeaterEnergySensor(HovalBaseEntity, RestoreEntity):
    """Cumulative energy consumed by the electric DHW heater (kWh).

    Rules:
    - Starts at 0.0 on first install; never returns to Unknown.
    - Restores previous total across HA restarts via RestoreEntity.
    - Only ever increases (TOTAL_INCREASING).
    - If heater state is Unknown (source dpIds not yet received), the
      counter holds its current value and does not accumulate.
    - 3-decimal precision.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:lightning-bolt-circle"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_electric_heater_energy"
        self._attr_name = "Electric Heater Energy"
        self._total_kwh: float = 0.0
        self._heater_on_since: datetime | None = None
        self._unsub_interval = None
        self._attr_native_value = 0.0   # always numeric, never None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Restore
        if (last := await self.async_get_last_state()) is not None:
            if last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE, None):
                try:
                    restored = float(last.state)
                    if restored >= 0.0:
                        self._total_kwh = restored
                        self._attr_native_value = round(self._total_kwh, 3)
                        _LOGGER.debug(
                            "Hoval CAN: restored electric_heater_energy = %.3f kWh",
                            self._total_kwh,
                        )
                except (ValueError, TypeError):
                    pass

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                heater_signal(self._entry.entry_id),
                self._handle_heater_change,
            )
        )
        self._unsub_interval = async_track_time_interval(
            self.hass, self._periodic_update, _ENERGY_UPDATE_INTERVAL
        )
        self.async_on_remove(self._cleanup)

    def _cleanup(self) -> None:
        if self._unsub_interval:
            self._unsub_interval()
            self._unsub_interval = None
        if self._heater_on_since is not None:
            self._flush(datetime.now(timezone.utc))

    @callback
    def _handle_heater_change(self) -> None:
        is_on = self._coordinator.electric_heater_on
        if is_on is None:
            return  # insufficient data — hold current value

        now = datetime.now(timezone.utc)
        if is_on and self._heater_on_since is None:
            self._heater_on_since = now
            _LOGGER.debug("Hoval CAN: Heizstab ON at %s", now.isoformat())
        elif not is_on and self._heater_on_since is not None:
            self._flush(now)
            _LOGGER.debug(
                "Hoval CAN: Heizstab OFF — total %.3f kWh", self._total_kwh
            )

        self._attr_native_value = self._running_total(now)
        self.async_write_ha_state()

    @callback
    def _periodic_update(self, now: datetime) -> None:
        if self._heater_on_since is not None:
            self._attr_native_value = self._running_total(now)
            self.async_write_ha_state()

    def _flush(self, now: datetime) -> None:
        if self._heater_on_since is not None:
            h = max(0.0, (now - self._heater_on_since).total_seconds() / 3600.0)
            self._total_kwh += h * HEATER_RATED_POWER_KW
            self._heater_on_since = None

    def _running_total(self, now: datetime) -> float:
        extra = 0.0
        if self._heater_on_since is not None:
            extra = (
                max(0.0, (now - self._heater_on_since).total_seconds() / 3600.0)
                * HEATER_RATED_POWER_KW
            )
        return round(self._total_kwh + extra, 3)


# ── Heat pump electrical: power sensor ────────────────────────────────────

class HovalHeatPumpElecPowerSensor(HovalBaseEntity):
    """Instantaneous electrical power drawn by the heat pump compressor.

    Calculation:  elec_kW = thermal_kW (DpId=29051) / COP

    COP is read from the integration's options (default 6.3, configurable).
    Returns Unknown until DpId=29051 has been received.
    3-decimal precision.
    """

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kW"
    _attr_icon = "mdi:heat-pump"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_heat_pump_electrical_power"
        self._attr_name = "Heat Pump Electrical Power"
        self._attr_native_value = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                dp_signal(self._entry.entry_id, DP_THERMAL_POWER),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        thermal_kw = self._coordinator.get_value(DP_THERMAL_POWER)
        if thermal_kw is None:
            self._attr_native_value = None
        else:
            cop = self._coordinator.cop
            self._attr_native_value = round(thermal_kw / cop, 3)
        self.async_write_ha_state()


# ── Heat pump electrical: cumulative energy sensor ─────────────────────────

class HovalHeatPumpElecEnergySensor(HovalBaseEntity, RestoreEntity):
    """Cumulative electrical energy consumed by the heat pump compressor (kWh).

    Integration method (left Riemann sum):
        ΔkWh = (thermal_kW_at_start_of_interval / COP) × elapsed_hours

    Integrates on every DpId=29051 update (approx. every 2 s from captures).
    If DpId=29051 becomes None (connection lost or null sentinel), the
    running period is abandoned and a fresh period starts on the next
    valid reading — no spurious energy from long gaps.

    Rules:
    - Starts at 0.0 on first install; never returns to Unknown.
    - Restores previous total across HA restarts.
    - Only ever increases.
    - 3-decimal precision.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:heat-pump"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_heat_pump_electrical_energy"
        self._attr_name = "Heat Pump Electrical Energy"
        self._total_kwh: float = 0.0
        self._last_thermal_kw: float | None = None
        self._last_ts: datetime | None = None
        self._attr_native_value = 0.0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        if (last := await self.async_get_last_state()) is not None:
            if last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE, None):
                try:
                    restored = float(last.state)
                    if restored >= 0.0:
                        self._total_kwh = restored
                        self._attr_native_value = round(self._total_kwh, 3)
                        _LOGGER.debug(
                            "Hoval CAN: restored heat_pump_electrical_energy"
                            " = %.3f kWh", self._total_kwh,
                        )
                except (ValueError, TypeError):
                    pass

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                dp_signal(self._entry.entry_id, DP_THERMAL_POWER),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        now = datetime.now(timezone.utc)
        thermal_kw = self._coordinator.get_value(DP_THERMAL_POWER)

        # Integrate PREVIOUS interval if both endpoints are valid
        if (
            self._last_thermal_kw is not None
            and self._last_ts is not None
            and thermal_kw is not None
        ):
            elapsed_h = max(
                0.0, (now - self._last_ts).total_seconds() / 3600.0
            )
            self._total_kwh += elapsed_h * (self._last_thermal_kw / self._coordinator.cop)

        # Update tracking state
        if thermal_kw is not None:
            self._last_thermal_kw = thermal_kw
            self._last_ts = now
        else:
            # Lost valid signal — reset so we don't accumulate a stale period
            self._last_thermal_kw = None
            self._last_ts = None

        self._attr_native_value = round(self._total_kwh, 3)
        self.async_write_ha_state()


# ── Total electrical: power sensor ────────────────────────────────────────

class HovalTotalElecPowerSensor(HovalBaseEntity):
    """Total instantaneous electrical power: heat pump + electric heater.

    Returns Unknown until both sources have been initialised.
    3-decimal precision.
    """

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kW"
    _attr_icon = "mdi:lightning-bolt"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_total_electrical_power"
        self._attr_name = "Total Electrical Power"
        self._attr_native_value = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        for signal in (
            dp_signal(self._entry.entry_id, DP_THERMAL_POWER),
            heater_signal(self._entry.entry_id),
        ):
            self.async_on_remove(
                async_dispatcher_connect(self.hass, signal, self._handle_update)
            )

    @callback
    def _handle_update(self) -> None:
        thermal_kw  = self._coordinator.get_value(DP_THERMAL_POWER)
        heater_on   = self._coordinator.electric_heater_on

        if thermal_kw is None or heater_on is None:
            self._attr_native_value = None
        else:
            hp_elec     = thermal_kw / self._coordinator.cop
            heater_elec = HEATER_RATED_POWER_KW if heater_on else 0.0
            self._attr_native_value = round(hp_elec + heater_elec, 3)

        self.async_write_ha_state()


# ── Total electrical: cumulative energy sensor ─────────────────────────────

class HovalTotalElecEnergySensor(HovalBaseEntity, RestoreEntity):
    """Total cumulative electrical energy: heat pump + electric heater (kWh).

    This sensor integrates TOTAL electrical power independently — it is not
    the sum of the two sub-sensors but its own persistent counter.  Keeping
    it independent means it works correctly even if either sub-sensor
    restarts, and it can be added to the HA Energy Dashboard separately.

    Integration: left Riemann sum, updated on every DpId=29051 or heater
    state change.  Gaps caused by None values reset the tracking state so
    no spurious energy is accumulated.

    Rules:
    - Starts at 0.0 on first install; never returns to Unknown.
    - Restores previous total across HA restarts.
    - Only ever increases.
    - 3-decimal precision.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:transmission-tower"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_total_electrical_energy"
        self._attr_name = "Total Electrical Energy"
        self._total_kwh: float = 0.0
        self._last_elec_kw: float | None = None
        self._last_ts: datetime | None = None
        self._unsub_interval = None
        self._attr_native_value = 0.0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        if (last := await self.async_get_last_state()) is not None:
            if last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE, None):
                try:
                    restored = float(last.state)
                    if restored >= 0.0:
                        self._total_kwh = restored
                        self._attr_native_value = round(self._total_kwh, 3)
                        _LOGGER.debug(
                            "Hoval CAN: restored total_electrical_energy"
                            " = %.3f kWh", self._total_kwh,
                        )
                except (ValueError, TypeError):
                    pass

        for signal in (
            dp_signal(self._entry.entry_id, DP_THERMAL_POWER),
            heater_signal(self._entry.entry_id),
        ):
            self.async_on_remove(
                async_dispatcher_connect(self.hass, signal, self._handle_update)
            )

        # Periodic update keeps displayed value fresh between heater transitions
        self._unsub_interval = async_track_time_interval(
            self.hass, self._periodic_update, _ENERGY_UPDATE_INTERVAL
        )
        self.async_on_remove(self._cleanup)

    def _cleanup(self) -> None:
        if self._unsub_interval:
            self._unsub_interval()
            self._unsub_interval = None
        # Flush any in-progress period on entity removal
        if self._last_elec_kw is not None and self._last_ts is not None:
            self._integrate(datetime.now(timezone.utc), None)

    @callback
    def _handle_update(self) -> None:
        now         = datetime.now(timezone.utc)
        thermal_kw  = self._coordinator.get_value(DP_THERMAL_POWER)
        heater_on   = self._coordinator.electric_heater_on

        if thermal_kw is None or heater_on is None:
            new_total_kw = None
        else:
            hp_elec      = thermal_kw / self._coordinator.cop
            heater_elec  = HEATER_RATED_POWER_KW if heater_on else 0.0
            new_total_kw = hp_elec + heater_elec

        self._integrate(now, new_total_kw)
        self._attr_native_value = round(self._total_kwh, 3)
        self.async_write_ha_state()

    @callback
    def _periodic_update(self, now: datetime) -> None:
        """Recompute and push a fresh running total every 60 s."""
        if self._last_elec_kw is None or self._last_ts is None:
            return
        # Integrate up to now using the last known power
        elapsed_h = max(0.0, (now - self._last_ts).total_seconds() / 3600.0)
        displayed  = round(self._total_kwh + elapsed_h * self._last_elec_kw, 3)
        if displayed != self._attr_native_value:
            self._attr_native_value = displayed
            self.async_write_ha_state()

    def _integrate(self, now: datetime, new_total_kw: float | None) -> None:
        """Commit the previous interval into _total_kwh, then update tracking."""
        if (
            self._last_elec_kw is not None
            and self._last_ts is not None
            and new_total_kw is not None
        ):
            elapsed_h = max(0.0, (now - self._last_ts).total_seconds() / 3600.0)
            self._total_kwh += elapsed_h * self._last_elec_kw

        if new_total_kw is not None:
            self._last_elec_kw = new_total_kw
            self._last_ts = now
        else:
            self._last_elec_kw = None
            self._last_ts = None
