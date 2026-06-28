"""Binary sensor platform for Hoval CAN."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass, BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
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
    async_add_entities([
        HovalElectricHeaterBinarySensor(coord, entry),
        HovalConnectivityBinarySensor(coord, entry),
    ])


class HovalElectricHeaterBinarySensor(BinarySensorEntity):
    """True when the electric DHW heater (Heizstab) is active.

    Detection: ON when all hold simultaneously:
      1. DHW Status == 8  (system is charging DHW tank)
      2. DHW Temperature < DHW Setpoint  (target not yet reached)
      3. Heat Generator Temp ≤ DHW Temp + 5 °C
         (heat pump generator not hot enough to heat tank — electric only)
      4. Compressor modulation ≤ 1 %  (compressor not running)

    Condition 4 reflects DHW priority: a single compressor cannot charge the
    tank and heat the house at once, so while the tank is charging a running
    compressor is itself doing the heating and the Heizstab is off. It only
    finishes the charge after the heat pump stops. This suppresses false ON
    pulses during the compressor's DHW-charge ramp, when the generator
    temperature lags and condition 3 alone would briefly read true.
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


class HovalConnectivityBinarySensor(BinarySensorEntity):
    """Diagnostic: gateway connection health.

    ON  = coordinator currently connected and receiving frames.
    OFF = disconnected / reconnecting (the watchdog forces this within
          STALE_TIMEOUT of a silent freeze, so OFF is observable instead of
          a silent stall).

    Stays *available* in both states so it can report OFF — an ICS alarm can
    trigger on this entity going OFF, or on `last_data_age_seconds` exceeding
    a threshold. Exposes reconnect count and last failure reason as attributes.
    """

    _attr_has_entity_name = True
    _attr_should_poll     = False
    _attr_device_class    = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon            = "mdi:lan-connect"

    def __init__(self, coord: HovalCANCoordinator, entry: ConfigEntry) -> None:
        self._coord = coord
        self._entry = entry
        self._attr_unique_id  = f"{entry.entry_id}_gateway_connection"
        self._attr_name       = "Gateway Connection"
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
                self.hass, connection_signal(self._entry.entry_id),
                lambda: self.async_write_ha_state(),
            )
        )

    @property
    def is_on(self) -> bool:
        return self._coord.connected

    @property
    def available(self) -> bool:
        # Always available: the whole point is to report the OFF state.
        return True

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        age = self._coord.last_data_age
        return {
            "last_data_age_seconds": None if age is None else round(age, 1),
            "reconnect_count": self._coord.reconnect_count,
            "framing_errors": self._coord.framing_errors,
            "last_error": self._coord.last_error,
        }
