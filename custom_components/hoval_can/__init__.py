"""Hoval CAN integration."""
from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import HovalCANCoordinator, HovalConfigEntry
from .health import HealthTracker

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

# HovalConfigEntry is defined in coordinator.py and re-exported here so the
# entry-point module still advertises the integration's public typing surface.
__all__ = ["HovalConfigEntry", "async_setup_entry", "async_unload_entry"]


async def async_setup_entry(hass: HomeAssistant, entry: HovalConfigEntry) -> bool:
    coordinator = HovalCANCoordinator(hass, entry)
    await coordinator.async_start()
    # Health tracker (v0.3.2): attached to the coordinator BEFORE the sensor
    # platform is forwarded so the health entities always find it. Owns its
    # own Store (…_health) and 5-minute sampling timer.
    health = HealthTracker(hass, entry, coordinator)
    await health.async_start()
    coordinator.health_tracker = health
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Entities are now subscribed to dispatcher signals — replay any state
    # restored from storage in coordinator.async_start() so it shows up
    # immediately instead of waiting for the next CAN broadcast.
    coordinator.async_replay_restored_signals()
    # NOTE (v0.4.0): no entry.add_update_listener() here. Reloading after an
    # options change is owned by HovalCANOptionsFlow(OptionsFlowWithReload).
    # Registering a listener *and* using a reloading config-flow helper is
    # deprecated since HA 2026.6 and an error from HA 2026.12.
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HovalConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = entry.runtime_data
        health: HealthTracker | None = getattr(
            coordinator, "health_tracker", None
        )
        if health is not None:
            await health.async_stop()   # cancels timer + final Store save
        await coordinator.async_stop()
    return unloaded
