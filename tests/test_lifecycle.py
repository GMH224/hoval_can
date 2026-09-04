"""Config-entry lifecycle tests: setup, unload and repeated reload.

Exercises ``hoval_can.async_setup_entry`` / ``async_unload_entry`` against
stubbed Home Assistant plumbing to confirm the v0.4.0 entry model:

  * the coordinator is published on ``entry.runtime_data`` (ICS runtime-data)
  * platforms are forwarded once, via ``async_forward_entry_setups``
  * unload stops the coordinator, the health tracker, the TCP task and both
    repeating timers
  * repeated option changes (setup -> unload -> setup ...) never accumulate
    timers or background tasks — the failure mode the removed update listener
    used to risk by double-reloading

    python3 tests/test_lifecycle.py         # exit code 0 == all pass
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

_FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [OK] {label}")
    else:
        suffix = f" — {detail}" if detail else ""
        print(f"  [FAIL] {label}{suffix}")
        _FAILURES.append(label)


# ── Instrumented stubs ────────────────────────────────────────────────────

LIVE_TIMERS: list[str] = []
LIVE_TASKS: list[asyncio.Task] = []


def _install():
    import test_protocol  # noqa: F401  (installs the base HA stubs)

    # Instrumented timer helper: every async_track_time_interval registers a
    # live timer; calling the returned unsub removes it. A leak shows up as a
    # non-empty LIVE_TIMERS after unload.
    def _track(hass, action, interval, *a, **k):
        token = f"timer@{getattr(action, '__name__', action)}"
        LIVE_TIMERS.append(token)

        def _unsub():
            if token in LIVE_TIMERS:
                LIVE_TIMERS.remove(token)

        return _unsub

    sys.modules["homeassistant.helpers.event"].async_track_time_interval = _track

    sys.path.insert(0, os.path.join(ROOT, "custom_components"))
    import importlib
    mod = importlib.import_module("hoval_can")

    # coordinator.py and health.py bind the helper at import time
    # (`from ... import async_track_time_interval`), so rebinding the helper
    # module alone is not enough -- patch the names they actually call.
    for name in ("hoval_can.coordinator", "hoval_can.health"):
        target = sys.modules.get(name)
        if target is not None and hasattr(target, "async_track_time_interval"):
            target.async_track_time_interval = _track
    return mod


class _Entry:
    """Stub config entry with the background-task helper HA provides."""

    entry_id = "e1"
    data = {"host": "192.0.2.10", "port": 3113}
    options: dict = {}
    title = "Hoval CAN (192.0.2.10)"
    version = 1
    unique_id = "192.0.2.10:3113"

    def async_create_background_task(self, hass, coro, name):
        task = asyncio.get_event_loop().create_task(coro, name=name)
        LIVE_TASKS.append(task)
        return task


class _ConfigEntries:
    def __init__(self):
        self.forwarded: list[tuple] = []
        self.unloaded: list[tuple] = []

    async def async_forward_entry_setups(self, entry, platforms):
        self.forwarded.append((entry.entry_id, tuple(platforms)))

    async def async_unload_platforms(self, entry, platforms):
        self.unloaded.append((entry.entry_id, tuple(platforms)))
        return True


def _hass():
    loop = asyncio.get_event_loop()
    return types.SimpleNamespace(
        loop=loop, data={}, config_entries=_ConfigEntries()
    )


async def _cycle(mod, hass, entry):
    """One full setup -> unload cycle."""
    ok = await mod.async_setup_entry(hass, entry)
    coord = entry.runtime_data
    # Let the connection task start and fail its first connect attempt.
    await asyncio.sleep(0)
    unloaded = await mod.async_unload_entry(hass, entry)
    return ok, coord, unloaded


def test_lifecycle() -> None:
    mod = _install()

    async def scenario():
        print("Entry setup:")
        hass = _hass()
        entry = _Entry()

        ok = await mod.async_setup_entry(hass, entry)
        check("async_setup_entry returns True", ok is True)
        check("coordinator is published on entry.runtime_data",
              getattr(entry, "runtime_data", None) is not None)
        check("runtime_data holds a HovalCANCoordinator",
              type(entry.runtime_data).__name__ == "HovalCANCoordinator",
              type(getattr(entry, "runtime_data", None)).__name__)
        check("health tracker is attached before platforms are forwarded",
              getattr(entry.runtime_data, "health_tracker", None) is not None)
        check("nothing was written to hass.data",
              hass.data == {}, str(hass.data))
        check("platforms forwarded exactly once",
              len(hass.config_entries.forwarded) == 1,
              str(hass.config_entries.forwarded))
        check("both platforms forwarded in one call",
              len(hass.config_entries.forwarded[0][1]) == 2,
              str(hass.config_entries.forwarded))

        await asyncio.sleep(0)
        timers_loaded = len(LIVE_TIMERS)
        check("repeating timers are running while loaded", timers_loaded >= 2,
              f"{timers_loaded} timer(s)")

        print("Entry unload:")
        coord = entry.runtime_data
        unloaded = await mod.async_unload_entry(hass, entry)
        check("async_unload_entry returns True", unloaded is True)
        check("platforms were unloaded", len(hass.config_entries.unloaded) == 1)
        check("coordinator is marked stopped", coord._stop is True)
        check("TCP task released", coord._task is None)
        check("rate timer unsubscribed", coord._rate_unsub is None)
        check("every timer was cancelled on unload",
              LIVE_TIMERS == [], f"leaked: {LIVE_TIMERS}")
        await asyncio.sleep(0)
        cancelled = [t for t in LIVE_TASKS if t.done() or t.cancelled()]
        check("background task is no longer running",
              len(cancelled) == len(LIVE_TASKS),
              f"{len(LIVE_TASKS) - len(cancelled)} still live")

        print("Repeated option changes (reload loop):")
        LIVE_TASKS.clear()
        hass2 = _hass()
        entry2 = _Entry()
        for i in range(5):
            ok, coord, unloaded = await _cycle(mod, hass2, entry2)
            if not (ok and unloaded):
                check(f"cycle {i + 1} completed", False)
                return
        check("5 setup/unload cycles all completed", True)
        check("no timer leaked across 5 reloads",
              LIVE_TIMERS == [], f"leaked: {LIVE_TIMERS}")
        await asyncio.sleep(0)
        live = [t for t in LIVE_TASKS if not (t.done() or t.cancelled())]
        check("no TCP task leaked across 5 reloads",
              not live, f"{len(live)} live task(s)")
        check("each reload forwarded platforms exactly once",
              len(hass2.config_entries.forwarded) == 5,
              str(len(hass2.config_entries.forwarded)))
        check("each reload unloaded platforms exactly once",
              len(hass2.config_entries.unloaded) == 5,
              str(len(hass2.config_entries.unloaded)))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(scenario())
    finally:
        for t in LIVE_TASKS:
            t.cancel()
        loop.close()


def main() -> int:
    test_lifecycle()
    print()
    if _FAILURES:
        print(f"RESULT: {len(_FAILURES)} FAILED")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, HERE)
    sys.exit(main())
