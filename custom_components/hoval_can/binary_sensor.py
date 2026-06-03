"""Binary sensor platform for Hoval CAN."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, connection_signal, heater_signal
from .coordinator import HovalCANCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HovalCANCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HovalElectricHeaterBinarySensor(coordinator, entry)])


class HovalElectricHeaterBinarySensor(BinarySensorEntity):
    """Binary sensor: True when the electric DHW heater (Heizstab) is active.

    Detection logic (winter-safe):
      ON when ALL of:
        1. DHW regulation status == 8  (system is charging DHW)
        2. DHW actual temperature < DHW setpoint  (target not yet reached)
        3. Heat generator temperature <= DHW temperature + margin
           (heat pump cannot be heating the DHW tank — it is not hot enough)

    This correctly handles winter: even when the heat pump runs for space
    heating at 40 °C, a 55 °C+ DHW tank remains hotter than the generator,
    so condition 3 fires and the electric heater is correctly detected.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.HEAT
    _attr_icon = "mdi:water-boiler"

    def __init__(
        self, coordinator: HovalCANCoordinator, entry: ConfigEntry
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_electric_heater_active"
        self._attr_name = "Electric Heater Active"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Hoval CAN",
            manufacturer="Hoval",
            model="WLAN Gateway",
            configuration_url=f"http://{entry.data['host']}",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                heater_signal(self._entry.entry_id),
                self._handle_update,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                connection_signal(self._entry.entry_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool | None:
        return self._coordinator.electric_heater_on

    @property
    def available(self) -> bool:
        return self._coordinator.connected
