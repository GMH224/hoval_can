"""Hoval CAN integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import HovalCANCoordinator
from .health import HealthTracker

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = HovalCANCoordinator(hass, entry)
    await coordinator.async_start()
    # Health tracker (v0.3.2): attached to the coordinator BEFORE the sensor
    # platform is forwarded so the health entities always find it. Owns its
    # own Store (…_health) and 5-minute sampling timer.
    health = HealthTracker(hass, entry, coordinator)
    await health.async_start()
    coordinator.health_tracker = health
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Entities are now subscribed to dispatcher signals — replay any state
    # restored from storage in coordinator.async_start() so it shows up
    # immediately instead of waiting for the next CAN broadcast.
    coordinator.async_replay_restored_signals()
    # Reload on options change (e.g. new heater power) — RestoreEntity
    # preserves all accumulated energy totals across the reload.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: HovalCANCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        health: HealthTracker | None = getattr(
            coordinator, "health_tracker", None
        )
        if health is not None:
            await health.async_stop()   # cancels timer + final Store save
        await coordinator.async_stop()
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    _LOGGER.debug("Hoval CAN: options changed — reloading %s", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)
