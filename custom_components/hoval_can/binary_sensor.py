"""Binary sensor platform for Hoval CAN."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass, BinarySensorEntity,
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
    coord: HovalCANCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HovalElectricHeaterBinarySensor(coord, entry)])


class HovalElectricHeaterBinarySensor(BinarySensorEntity):
    """True when the electric DHW heater (Heizstab) is active.

    Detection: ON when all three hold simultaneously:
      1. DHW Status == 8  (system is charging DHW tank)
      2. DHW Temperature < DHW Setpoint  (target not yet reached)
      3. Heat Generator Temp ≤ DHW Temp + 5 °C
         (heat pump generator not hot enough to heat tank — electric only)

    Winter-safe: even when the heat pump runs at 40 °C for space heating,
    a 55 °C+ DHW tank is hotter than the generator; condition 3 fires.
    """

    _attr_has_entity_name = True
    _attr_should_poll     = False
    _attr_device_class    = BinarySensorDeviceClass.HEAT
    _attr_icon            = "mdi:water-boiler"

    def __init__(self, coord: HovalCANCoordinator, entry: ConfigEntry) -> None:
        self._coord = coord
        self._entry = entry
        self._attr_unique_id  = f"{entry.entry_id}_electric_heater_active"
        self._attr_name       = "Electric Heater Active"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Hoval CAN",
            manufacturer="Hoval",
            model="WLAN Gateway",
            configuration_url=f"http://{entry.data['host']}",
        )

    async def async_added_to_hass(self) -> None:
        for sig in (
            heater_signal(self._entry.entry_id),
            connection_signal(self._entry.entry_id),
        ):
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass, sig,
                    lambda: self.async_write_ha_state(),
                )
            )

    @property
    def is_on(self) -> bool | None:
        return self._coord.electric_heater_on

    @property
    def available(self) -> bool:
        return self._coord.connected
