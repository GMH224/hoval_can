"""Downloadable diagnostics for the Hoval CAN integration.

Exposed via Settings → Devices & Services → (entry) → "Download diagnostics".
Returns a redacted JSON snapshot of connection health, configured options, the
derived states, and the last-seen value of every decoded datapoint — enough to
triage an incident without shell access. Host/IP and the entry's unique_id are
redacted.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .coordinator import HovalCANCoordinator, HovalConfigEntry

TO_REDACT = {"host", "unique_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HovalConfigEntry
) -> dict[str, Any]:
    # runtime_data is only present while the entry is loaded; diagnostics can
    # legitimately be requested for an entry that failed to set up.
    coord: HovalCANCoordinator | None = getattr(entry, "runtime_data", None)

    entry_info = {
        "title": entry.title,
        "version": entry.version,
        "unique_id": entry.unique_id,
        "options": dict(entry.options),
        "data": dict(entry.data),
    }

    snapshot: dict[str, Any] = (
        coord.diagnostics_snapshot() if coord is not None
        else {"error": "coordinator not loaded"}
    )

    # Health-index snapshot (v0.3.2): latest fused statistics, confidence
    # breakdown, and the last 14 day-records — enough to audit a surprising
    # T² value without shell access.
    tracker = getattr(coord, "health_tracker", None)
    health: dict[str, Any] = (
        tracker.snapshot() if tracker is not None
        else {"error": "health tracker not loaded"}
    )

    return async_redact_data(
        {"entry": entry_info, "coordinator": snapshot, "health": health},
        TO_REDACT,
    )
