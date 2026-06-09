"""Hoval CAN integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import HovalCANCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = HovalCANCoordinator(hass, entry)
    await coordinator.async_start()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Reload on options change (e.g. new heater power) — RestoreEntity
    # preserves all accumulated energy totals across the reload.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: HovalCANCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_stop()
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    _LOGGER.debug("Hoval CAN: options changed — reloading %s", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)
