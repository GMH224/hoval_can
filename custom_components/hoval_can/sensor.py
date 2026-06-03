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
    SENSOR_DESCRIPTIONS,
    connection_signal,
    dp_signal,
    heater_signal,
)
from .coordinator import HovalCANCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HovalCANCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []

    # Standard dpId-based sensors
    for desc in SENSOR_DESCRIPTIONS:
        entities.append(HovalSensor(coordinator, entry, desc))

    # Derived electric heater sensors
    entities.append(HovalElectricHeaterPowerSensor(coordinator, entry))
    entities.append(HovalElectricHeaterEnergySensor(coordinator, entry))

    async_add_entities(entities)


# ── Shared device info ────────────────────────────────────────────────────

def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Hoval CAN",
        manufacturer="Hoval",
        model="WLAN Gateway",
        configuration_url=f"http://{entry.data['host']}",
    )


# ── Base entity ───────────────────────────────────────────────────────────

class HovalBaseEntity(SensorEntity):
    """Common base for all Hoval CAN sensor entities."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: HovalCANCoordinator, entry: ConfigEntry) -> None:
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


# ── Standard dpId sensor ──────────────────────────────────────────────────

class HovalSensor(HovalBaseEntity):
    """Sensor for a single DatapointId from the CAN-BUS stream."""

    def __init__(
        self,
        coordinator: HovalCANCoordinator,
        entry: ConfigEntry,
        desc,                        # HovalSensorDescription
    ) -> None:
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
        # Start as None → HA shows "Unknown" until first reading arrives
        self._attr_native_value = None

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


# ── Electric heater: power sensor ─────────────────────────────────────────

class HovalElectricHeaterPowerSensor(HovalBaseEntity):
    """Instantaneous power of the electric DHW heater (0 or rated kW).

    Value is HEATER_RATED_POWER_KW when the heater is detected as active,
    0.0 otherwise.  Returns None (Unknown) before sufficient data arrives
    to determine the heater state.
    """

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kW"
    _attr_icon = "mdi:water-boiler"

    def __init__(self, coordinator: HovalCANCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_electric_heater_power"
        self._attr_name = "Electric Heater Power"
        self._attr_native_value = None

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
        self._attr_native_value = (
            HEATER_RATED_POWER_KW if state else (0.0 if state is not None else None)
        )
        self.async_write_ha_state()


# ── Electric heater: cumulative energy sensor ─────────────────────────────

class HovalElectricHeaterEnergySensor(HovalBaseEntity, RestoreEntity):
    """Cumulative electric energy consumed by the Heizstab (kWh).

    Calculation:
        energy += HEATER_RATED_POWER_KW × elapsed_hours
        (integrated precisely from heater-on to heater-off timestamps)

    Persistence: the accumulated kWh value survives HA restarts via
    RestoreEntity.  The counter is TOTAL_INCREASING (never decreases).

    Any partial period at the moment of a restart is not counted; this
    represents at most a few kWh per year of under-counting and is
    acceptable for a non-billing energy estimate.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:lightning-bolt-circle"

    def __init__(self, coordinator: HovalCANCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_electric_heater_energy"
        self._attr_name = "Electric Heater Energy"
        self._total_kwh: float = 0.0
        self._heater_on_since: datetime | None = None
        self._unsub_interval = None
        # Before restore: show None (Unknown)
        self._attr_native_value = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # ── Restore previous accumulated energy ──────────────────────────
        last = await self.async_get_last_state()
        if last is not None and last.state not in (
            STATE_UNKNOWN, STATE_UNAVAILABLE, None
        ):
            try:
                self._total_kwh = float(last.state)
                self._attr_native_value = round(self._total_kwh, 3)
                _LOGGER.debug(
                    "Hoval CAN: restored electric heater energy: %.3f kWh",
                    self._total_kwh,
                )
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "Hoval CAN: could not restore heater energy from state %r",
                    last.state,
                )

        # ── Subscribe to heater state changes ────────────────────────────
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                heater_signal(self._entry.entry_id),
                self._handle_heater_change,
            )
        )

        # ── Periodic display update (every 60 s) ─────────────────────────
        # Shows a "live running total" while the heater is on without
        # waiting for it to turn off.
        self._unsub_interval = async_track_time_interval(
            self.hass, self._periodic_update, timedelta(seconds=60)
        )
        self.async_on_remove(self._cleanup_interval)

    def _cleanup_interval(self) -> None:
        if self._unsub_interval:
            self._unsub_interval()
            self._unsub_interval = None
        # Flush any partial period on removal
        if self._heater_on_since is not None:
            self._flush_period(datetime.now(timezone.utc))

    @callback
    def _handle_heater_change(self) -> None:
        """Called when electric_heater_on changes."""
        now = datetime.now(timezone.utc)
        is_on = self._coordinator.electric_heater_on

        if is_on and self._heater_on_since is None:
            # Heater just turned ON → record start time
            self._heater_on_since = now
            _LOGGER.debug("Hoval CAN: electric heater ON at %s", now.isoformat())

        elif not is_on and self._heater_on_since is not None:
            # Heater just turned OFF → flush the period
            self._flush_period(now)
            _LOGGER.debug(
                "Hoval CAN: electric heater OFF. Total: %.3f kWh", self._total_kwh
            )

        self._attr_native_value = self._running_total(now)
        self.async_write_ha_state()

    @callback
    def _periodic_update(self, now: datetime) -> None:  # called with aware dt
        """Update displayed value every minute while heater is on."""
        if self._heater_on_since is not None:
            self._attr_native_value = self._running_total(now)
            self.async_write_ha_state()

    def _flush_period(self, now: datetime) -> None:
        """Add elapsed energy for the period heater_on_since → now."""
        if self._heater_on_since is not None:
            elapsed_h = (now - self._heater_on_since).total_seconds() / 3600.0
            self._total_kwh += elapsed_h * HEATER_RATED_POWER_KW
            self._heater_on_since = None

    def _running_total(self, now: datetime) -> float:
        """Return committed energy + current ongoing period (if any)."""
        extra = 0.0
        if self._heater_on_since is not None:
            extra = (
                (now - self._heater_on_since).total_seconds()
                / 3600.0
                * HEATER_RATED_POWER_KW
            )
        return round(self._total_kwh + extra, 3)
