# Hoval CAN 0.4.0 — ICS Quality & Home Assistant Deprecation Audit

**Baseline:** Home Assistant Core 2026.9
**Predecessor:** 0.3.3
**Audit date:** 2026-09-04
**Evidence:** repository source, plus a live production log
(`home-assistant_2026-09-04T09-58-07_636Z.log`, HA 2026.9, Python 3.14)

---

## Executive summary

> **0.3.3 was crashing in production on HA 2026.9, and would have become
> unloadable in HA 2026.12.** Neither problem was identified by the
> previously supplied audit (`ICS_QUALITY_DEPRECATION_AUDIT_HOVAL_CAN_0_3_3.md`),
> which concluded that the code was clear of breaking changes and that only a
> stylistic options-flow refactor was advisable.
>
> This release fixes both, migrates the entry to `runtime_data`, removes two
> deprecated import paths, and adds three new test suites — including a
> regression test that reproduces the production crash and is verified to fail
> against 0.3.3.

| # | Defect | Status in 0.3.3 | Severity |
|---|---|---|---|
| **F-01** | `async_write_ha_state()` called from an executor thread | **Crashing now** on 2026.9 | **Critical** |
| **F-02** | Update listener + reloading config-flow helper | **Error from 2026.12** | **High** |
| F-03 | `DeviceInfo` imported from `helpers.entity` | Legacy path | Medium |
| F-04 | `AddEntitiesCallback` on config-entry platforms | Superseded | Medium |
| F-05 | Runtime state in `hass.data` | ICS Bronze gap | Medium |
| F-06 | No `integration_type`; HACS floor `2023.1.0` | Metadata gap / latent bug | Medium |

---

## 1. Corrections to the supplied 0.3.3 audit

This section exists because acting on the supplied audit alone would have left
a running system broken. Each correction is stated with the evidence that
settles it.

### 1.1 "No API scheduled to stop working in 2026.9–2026.12" — **incorrect**

The supplied audit's executive result, and again its §13 ("Confirmed affected:
**None**"), state that no breaking API is used.

Home Assistant's developer blog *Deprecating config entry listener with
reloading methods in config flow* (2026-05-07) states that from Core 2026.6,
using a config entry listener **together with any reloading method in a config
flow** is deprecated and becomes an **error from 2026.12**. The prescribed
remedies are to remove the listener, use `async_update_and_abort()` in place of
`async_update_reload_and_abort()`, and pass `reload_on_update=False` to
`_abort_if_unique_id_configured()`.

0.3.3 contained **both halves** of that combination:

- `__init__.py:35` — `entry.async_on_unload(entry.add_update_listener(_async_reload_entry))`
- `config_flow.py:77` — `self._abort_if_unique_id_configured()`, with the
  default `reload_on_update=True`

The integration was therefore directly affected. The supplied audit reached the
opposite conclusion because §2.3 asserts the helper is not used —

> "The supplied integration does **not** use either of those reload helpers."

— while its own §4.1 correctly records the opposite:

> "`config_flow.py:76-77` creates a unique ID from `host:port` and calls
> `_abort_if_unique_id_configured()`."

The document contradicts itself, and the branch it acted on was the wrong one.
The recommendation it did make (`OptionsFlowWithReload`) was correct, but it
was presented as optional polish rather than as the fix for a scheduled hard
error, and it was incomplete: `OptionsFlowWithReload` alone does not clear the
`_abort_if_unique_id_configured()` half.

### 1.2 The thread-safety crash — **not identified at all**

The supplied audit reviewed `sensor.py` and `binary_sensor.py` in several
sections and found them clean. Both files contained a defect that HA 2026.9
converts into a `RuntimeError`. See §2 below. This was not a forward-looking
risk: it was already firing in the supplied log.

### 1.3 Deprecated import paths — **not identified**

The audit inventories imports in §3 and declares direct helper imports clean.
Two import paths in `sensor.py` and `binary_sensor.py` are legacy
(§4). `helpers.entity.DeviceInfo` in particular is a compatibility re-export;
the canonical location is `helpers.device_registry`.

### 1.4 HACS minimum version — **not reviewed**

`hacs.json` declared `"homeassistant": "2023.1.0"`. The audit's own primary
recommendation (`OptionsFlowWithReload`) requires 2025.7+. Adopting the
recommendation without raising the floor would have shipped a release to cores
that cannot import it. `hacs.json` is absent from the audit's file-by-file
change list (§15).

### 1.5 Items where the supplied audit was correct

Credit where due, and adopted in this release:

- `ConfigEntry.runtime_data` migration (Bronze `runtime-data`) — adopted.
- `OptionsFlowWithReload` — adopted, with the missing second half added.
- Dropping the private `_config_entry` reference for `self.config_entry` — adopted.
- Explicit `integration_type` — adopted.
- Config-flow test coverage — adopted, and extended.
- Device registry APIs, entity-ID assignment, `async_forward_entry_setups`,
  time-interval helpers, diagnostics redaction, unit constants: independently
  re-verified as clean (§6).

---

## 2. F-01 — `async_write_ha_state()` from an executor thread (Critical)

### 2.1 Evidence

From the supplied production log, HA 2026.9:

```
2026-09-03 21:21:35.562 ERROR (SyncWorker_7) [homeassistant.util.logging]
Exception in <lambda> when dispatching 'hoval_can_01KT…_electric_heater': ()
  File "/config/custom_components/hoval_can/binary_sensor.py", line 74, in <lambda>
    lambda: self.async_write_ha_state(),
RuntimeError: Detected that custom integration 'hoval_can' calls
async_write_ha_state from a thread other than the event loop, which may cause
Home Assistant to crash or data to corrupt.
```

The reporting thread is `SyncWorker_7` — a thread-pool worker, not `MainThread`.
The same condition is also reported at startup by `homeassistant.helpers.frame`.

### 2.2 Mechanism

Home Assistant assigns a **job type** to every callable passed to
`async_dispatcher_connect`:

| Target | Inferred type | Where it runs |
|---|---|---|
| coroutine function | `Coroutinefunction` | awaited on the event loop |
| `@callback`-decorated | `Callback` | inline on the event loop |
| **anything else** | **`Executor`** | **thread pool** |

The HA developer documentation on thread safety states this directly: if the
`@callback` decorator is missing, the handler is run in the executor, which
makes a non-thread-safe call to `async_write_ha_state`.

A bare `lambda: self.async_write_ha_state()` is neither a coroutine nor
`@callback`-marked, so it lands in the third row. The dispatcher hands it to a
`SyncWorker` thread, and the entity writes state off-loop. Under HA 2026.9 the
report behaviour for custom integrations is `ReportBehavior.ERROR`, so this
raises instead of warning.

### 2.3 Affected sites in 0.3.3

| File | Line | Scope |
|---|---:|---|
| `sensor.py` | 101 | `HovalBaseEntity` — **base class of every sensor entity** (~40 entities) |
| `binary_sensor.py` | 74 | `HovalElectricHeaterBinarySensor` (heater + connection signals) |
| `binary_sensor.py` | 123 | `HovalConnectivityBinarySensor` (connection signal) |

The `sensor.py` occurrence is the most serious: it is inherited, so every
sensor subscribed an executor-bound target to the connection signal. Every
connect/disconnect transition dispatched a burst of off-loop state writes.

The `@callback`-decorated `_update` methods elsewhere in `sensor.py` were
always correct — this was specifically the untyped-lambda pattern.

### 2.4 Fix

Each lambda is replaced by a `@callback`-decorated bound method:

```python
@callback
def _async_signal_write_state(self) -> None:
    """Dispatcher target: write state from the event loop.

    MUST stay decorated with @callback. …
    """
    self.async_write_ha_state()
```

`binary_sensor.py` additionally imports `callback` from `homeassistant.core`.

### 2.5 Why the existing suite missed it

`tests/test_protocol.py` stubs the HA core module with:

```python
_mod("homeassistant.core", HomeAssistant=…, callback=lambda f: f)
```

The real decorator sets a marker attribute that job-type inference reads; the
identity-function stub discards it. Under that stub a lambda and a `@callback`
are indistinguishable, so no protocol-level test could ever have detected the
difference. This is corrected in §7.1.

---

## 3. F-02 — Config-entry listener plus reloading config-flow helper (High)

### 3.1 What 0.3.3 did

```python
# __init__.py:35
entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

# __init__.py:52-54
async def _async_reload_entry(hass, entry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)

# config_flow.py:77
self._abort_if_unique_id_configured()      # reload_on_update defaults to True
```

Two independent mechanisms could reload the same entry, which is the
double-reload / race condition the deprecation targets.

### 3.2 Fix

Three coordinated changes — all three are required:

1. **`config_flow.py`** — `class HovalCANOptionsFlow(OptionsFlowWithReload)`.
   The framework now owns the post-options reload.
2. **`config_flow.py`** — `self._abort_if_unique_id_configured(reload_on_update=False)`.
   This is the half `OptionsFlowWithReload` does not address, and the half the
   supplied audit omitted.
3. **`__init__.py`** — the listener registration and `_async_reload_entry()` are
   deleted, with a comment recording why they must not return.

The options handler also drops its `__init__`/`_config_entry` in favour of the
framework-provided `self.config_entry` property, and
`async_get_options_flow()` now constructs `HovalCANOptionsFlow()` with no
argument (`config_entry` is a read-only property on the base class).

---

## 4. F-03 / F-04 — Deprecated import paths

| Was | Now |
|---|---|
| `from homeassistant.helpers.entity import DeviceInfo` | `from homeassistant.helpers.device_registry import DeviceInfo` |
| `from homeassistant.helpers.entity_platform import AddEntitiesCallback` | `… import AddConfigEntryEntitiesCallback` |

`AddConfigEntryEntitiesCallback` is the correct type for a platform set up from
a config entry, and is what the current developer documentation's reference
integration uses. Both platform `async_setup_entry` signatures are updated.

---

## 5. F-05 / F-06 — ICS quality

### 5.1 `runtime_data` (Bronze `runtime-data`)

`hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator` is replaced by
`entry.runtime_data = coordinator`, with a typed alias declared in
`coordinator.py`:

```python
type HovalConfigEntry = ConfigEntry["HovalCANCoordinator"]
```

The alias lives beside the coordinator rather than in `__init__.py` so the
platform modules import it without importing the package entry point. It is
re-exported from `__init__.py` via `__all__`.

Consumers updated: `sensor.py`, `binary_sensor.py`, `diagnostics.py`.
`async_unload_entry()` no longer pops a dictionary key — the coordinator's
lifetime is now exactly the entry's lifetime, so no cleanup can be missed.

`diagnostics.py` reads `getattr(entry, "runtime_data", None)`, because
`runtime_data` is absent (not `None`) on an entry that failed to load;
diagnostics must still return entry metadata in that case.

### 5.2 Manifest and HACS metadata

- `"integration_type": "device"` added — one entry represents one gateway.
- `"version": "0.4.0"`.
- Keys reordered to hassfest's rule: `domain`, `name`, then alphabetical.
- `hacs.json` floor raised `2023.1.0` → `2025.12.0`. The old value was a latent
  packaging bug: it offered the integration to cores that cannot import
  `OptionsFlowWithReload` (2025.7+) or `AddConfigEntryEntitiesCallback` (2025.2+).

---

## 6. Independent deprecation re-verification

Re-checked against the source rather than inherited from the previous audit.
All clean in 0.4.0:

| Pattern | Result |
|---|---|
| `hass.helpers.*` / `hass.components.*` | not used |
| `async_forward_entry_setup` (singular) | not used — plural form correct |
| `via_device=` in device registry calls | not used |
| `DeviceInfo.default_name` / `default_model` / `default_manufacturer` | not used |
| `DeviceRegistry.async_get_device()`, registry mutation | not used |
| direct `self.entity_id = …` assignment | not used |
| `async_track_state_change()` (legacy) | not used |
| `get_astral_location()` | not used |
| deprecated unit / device-class constants | not used — enum APIs throughout |
| `async_update_reload_and_abort()` | not used |
| runtime state in `hass.data` | **removed in 0.4.0** |
| lambda as dispatcher target | **removed in 0.4.0** |

**Note on `hoval_connect`.** The supplied log also shows deprecation warnings
for a *separate* custom integration, `hoval_connect`, calling
`device_registry.async_get_or_create` with `via_device` (removal 2027.8). That
is a different codebase with its own issue tracker
(`hoval-connect-api`) and is out of scope here. `hoval_can` does not use
`via_device`.

---

## 7. Test additions

Suite grew from 2 files / ~241 checks to 5 files / **317 checks**.

### 7.1 `tests/test_thread_safety.py` (new, 13 checks)

Directly targets F-01. It installs a **faithful** `callback` stub that sets the
real marker attribute, reimplements HA's job-type inference, and then:

1. verifies the inference model itself (a lambda is an `Executor` job);
2. statically scans all 20 `async_dispatcher_connect` registrations for lambdas;
3. asserts `HovalBaseEntity._async_signal_write_state` exists and is `@callback`;
4. instantiates the real binary-sensor entities, records every target they
   register, and asserts none is executor-bound — including an explicit check
   on the `_electric_heater` signal named in the production traceback.

### 7.2 `tests/test_config_flow.py` (new, 43 checks)

- **Functional:** default/custom port, host whitespace stripping, `cannot_connect`
  on an unreachable gateway, `already_configured` on a duplicate, `unique_id`
  shape, port bounds (0/-1/65536 rejected, 3113 accepted), options pre-fill from
  stored values and defaults, all seven options present so saving one cannot drop
  the others, and full persistence on submit. The connection probe is patched
  rather than opening a socket.
- **Lifecycle contracts (AST-based, so they cannot silently regress):** no
  `add_update_listener`; `reload_on_update=False` present; `OptionsFlowWithReload`
  subclassed and plain `OptionsFlow` not; no `_config_entry`; no `hass.data`
  runtime state; `runtime_data` assigned and read defensively; modern import
  paths; manifest version/type/key-order; HACS floor ≥ 2025.7.

### 7.3 `tests/test_lifecycle.py` (new, 20 checks)

End-to-end `async_setup_entry` → `async_unload_entry` against instrumented
stubs: `runtime_data` published, health tracker attached before platforms are
forwarded, nothing written to `hass.data`, platforms forwarded exactly once,
and on unload the coordinator stopped, TCP task released, and **both** repeating
timers cancelled. Then **five consecutive setup/unload cycles** assert no timer
and no background task leaks — the accumulation the removed double-reload path
used to risk.

### 7.4 `tests/run_all.py` (new)

Each suite installs its own `sys.modules` stubs, so they cannot share a
process. The runner spawns one subprocess per suite and aggregates.

### 7.5 Updated existing suites

- `test_protocol.py`: stubs extended for `helpers.device_registry` and
  `AddConfigEntryEntitiesCallback` (legacy names retained); the diagnostics test
  now injects via `entry.runtime_data`, and the "coordinator missing" case now
  models a genuinely unloaded entry with **no** `runtime_data` attribute,
  additionally asserting entry metadata is still returned.

### 7.6 Negative controls — the tests are proven, not assumed

Every new suite was run against **unmodified 0.3.3**. A regression test that
cannot detect the bug is decoration, so detection power was verified:

- `test_thread_safety` fails on 0.3.3 at `sensor.py:99`, `binary_sensor.py:72`,
  `binary_sensor.py:121` — the exact sites in the production traceback.
- `test_config_flow` fails all 21 lifecycle contracts on 0.3.3.

Both pass on 0.4.0.

### 7.7 Result

```
[PASS] test_protocol         163 checks
[PASS] test_health            78 checks
[PASS] test_thread_safety     13 checks
[PASS] test_config_flow       43 checks
[PASS] test_lifecycle         20 checks
5/5 suites passed, 317 checks total
```

`ruff check --select E9,F,W6` clean across `custom_components/` and `tests/`.

---

## 8. Verification checklist

| Item | Status |
|---|---|
| No `async_write_ha_state` from a non-loop thread | **Fixed** — F-01 |
| No config-entry update listener | **Fixed** — F-02 |
| `_abort_if_unique_id_configured(reload_on_update=False)` | **Fixed** — F-02 |
| Options flow reload owned by the framework | **Fixed** — `OptionsFlowWithReload` |
| Options flow saves all seven values | Tested |
| Repeated option changes create no duplicate timers | Tested — 5 cycles |
| Repeated option changes create no duplicate TCP tasks | Tested — 5 cycles |
| Unload cancels TCP task / rate timer / health timer | Tested |
| No `hass.data` runtime state | **Fixed** — F-05 |
| Diagnostics work loaded, degrade gracefully unloaded | Tested |
| No deprecated device-registry API | Verified clean |
| No direct `entity_id` assignment | Verified clean |
| Config-flow tests present | **Added** — 43 checks |
| Ruff clean | Yes |
| RestoreEntity energy history survives reload | Unchanged from 0.3.3 |
| Verified on a real HA 2026.9 instance | **Owner action — see §10** |
| Verified on a 2026.12 beta | **Owner action — pending release** |

---

## 9. Deliberately not done

Both would be user-visible breaking changes, inappropriate to bundle into a
stability release.

**Device identifiers.** The supplied audit suggests moving from
`identifiers={(DOMAIN, entry.entry_id)}` to a hardware serial. Changing an
identifier orphans every existing entity and its accumulated energy history.
The gateway is not confirmed to expose a stable unique ID over this protocol.
Not changed without that confirmation and an explicit migration.

**Entity translation keys.** Migrating ~23 hard-coded `_attr_name` values to
`translation_key` renames entities and breaks dashboards, automations and
long-term statistics. Correct for Gold, but it needs its own release with a
migration path and release notes.

Also unchanged: discovery (no confirmed mDNS/DHCP signature — speculative
matchers cause false positives) and a reconfigure flow (a genuine Gold gap,
deferred to keep this release focused).

---

## 10. Recommended deployment

1. Install 0.4.0 and restart Home Assistant.
2. Search the log for `hoval_can`. The expected result is **only** the standard
   "custom integration … has not been tested by Home Assistant" notice.
   The `RuntimeError` at `binary_sensor.py` and the
   `homeassistant.helpers.frame` thread-safety warning must both be gone.
3. Open **Configure**, change one option, save. Confirm the entities reload once
   and that accumulated energy totals survive.
4. Re-test on the first 2026.12 beta.

Unrelated items visible in the same log and worth separate attention: a
duplicate automation ID (`hoval_sliding_solar_boost_configurable_yaml`), and the
third-party `hoval_connect` `via_device` deprecation (removal 2027.8).

---

## 11. Sources

1. *Deprecating config entry listener with reloading methods in config flow* —
   developers.home-assistant.io, 2026-05-07
2. *Thread safety with asyncio* — developers.home-assistant.io
3. *Options flow* — developers.home-assistant.io/docs/core/integration/options_flow
4. *Entity* (reference integration) — developers.home-assistant.io/docs/core/entity
5. *More device registry deprecations, new helpers and validation* — 2026-08-24
6. *Integration Quality Scale rules* — developers.home-assistant.io
7. Production log `home-assistant_2026-09-04T09-58-07_636Z.log` (HA 2026.9)

---

## Final assessment

**2026.9:** 0.3.3 was actively raising `RuntimeError` on every connection-state
transition. 0.4.0 removes the cause.

**2026.12:** 0.3.3 used the listener-plus-reloading-helper combination that
becomes an error. 0.4.0 removes both halves.

**ICS quality:** Bronze `runtime-data` satisfied; config-flow and lifecycle
coverage added with verified detection power; manifest and HACS metadata
corrected. Remaining gaps to Gold are entity translations, a reconfigure flow
and end-user documentation — all non-breaking and deferrable.
