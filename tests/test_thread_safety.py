"""Thread-safety regression tests for dispatcher targets.

Reproduces the production failure observed on Home Assistant 2026.9:

    RuntimeError: Detected that custom integration 'hoval_can' calls
    async_write_ha_state from a thread other than the event loop
    ... at custom_components/hoval_can/binary_sensor.py, line 74:
    lambda: self.async_write_ha_state(),

Root cause
----------
Home Assistant infers a *job type* for every callable handed to
``async_dispatcher_connect``:

    coroutine function      -> awaited on the event loop
    @callback decorated     -> invoked inline on the event loop
    anything else           -> HassJobType.Executor, run in the thread pool

A bare ``lambda: self.async_write_ha_state()`` falls into the third bucket, so
the dispatcher runs it on a ``SyncWorker`` thread and ``async_write_ha_state``
is reached off-loop. HA 2026.9 raises RuntimeError for custom integrations in
that situation.

Why the pre-existing suite missed it
------------------------------------
``tests/test_protocol.py`` stubs ``homeassistant.core.callback`` as
``lambda f: f``, which throws away the marker attribute that job-type
inference reads. Under that stub a lambda and a @callback are
indistinguishable. These tests install a *faithful* stub instead: it sets
``_hass_callback = True`` exactly like the real decorator, then asserts the
inferred job type of every dispatcher target the integration registers.

    python3 tests/test_thread_safety.py     # exit code 0 == all pass
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
COMPONENT = os.path.join(ROOT, "custom_components", "hoval_can")

_FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [OK] {label}")
    else:
        suffix = f" — {detail}" if detail else ""
        print(f"  [FAIL] {label}{suffix}")
        _FAILURES.append(label)


# ── Faithful reimplementation of HA's job-type inference ──────────────────
# Mirrors homeassistant.core.get_hassjob_callable_job_type().

CALLBACK = "Callback"
COROUTINE = "Coroutinefunction"
EXECUTOR = "Executor"


def ha_callback(func):
    """Faithful stand-in for homeassistant.core.callback."""
    func._hass_callback = True  # noqa: SLF001
    return func


def infer_job_type(target) -> str:
    """Return the HassJobType HA would infer for `target`."""
    check_target = target
    while isinstance(check_target, types.MethodType):
        check_target = check_target.__func__
    if asyncio.iscoroutinefunction(check_target):
        return COROUTINE
    if getattr(check_target, "_hass_callback", False) is True:
        return CALLBACK
    return EXECUTOR


# ── 1. The inference model itself ─────────────────────────────────────────

def test_inference_model() -> None:
    print("Job-type inference model:")

    @ha_callback
    def decorated() -> None: ...

    async def coro() -> None: ...

    def plain() -> None: ...

    check("a @callback is a Callback job",
          infer_job_type(decorated) == CALLBACK)
    check("a coroutine function is a Coroutinefunction job",
          infer_job_type(coro) == COROUTINE)
    check("a plain function is an Executor job",
          infer_job_type(plain) == EXECUTOR)
    check("a bare lambda is an Executor job (the 0.3.3 bug)",
          infer_job_type(lambda: None) == EXECUTOR)

    class Ent:
        @ha_callback
        def method(self) -> None: ...

    check("a bound @callback method is a Callback job",
          infer_job_type(Ent().method) == CALLBACK)


# ── 2. Static guard: no bare lambda may be a dispatcher target ────────────

def _dispatcher_target_nodes(tree: ast.AST):
    """Yield (lineno, arg_node) for the target argument of every
    async_dispatcher_connect(...) call."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name != "async_dispatcher_connect":
            continue
        # signature: (hass, signal, target)
        if len(node.args) >= 3:
            yield node.lineno, node.args[2]


def test_no_lambda_dispatcher_targets() -> None:
    print("Static scan of dispatcher registrations:")
    total = 0
    offenders: list[str] = []
    for fname in ("sensor.py", "binary_sensor.py"):
        path = os.path.join(COMPONENT, fname)
        tree = ast.parse(open(path, encoding="utf-8").read())
        for lineno, arg in _dispatcher_target_nodes(tree):
            total += 1
            if isinstance(arg, ast.Lambda):
                offenders.append(f"{fname}:{lineno}")
    check("found dispatcher registrations to inspect", total > 0,
          f"found {total}")
    check(f"no lambda is used as a dispatcher target ({total} registrations)",
          not offenders, ", ".join(offenders))


# ── 3. Every registered target is loop-safe, on the real classes ──────────

def test_registered_targets_are_loop_safe() -> None:
    """Import the real entity modules against faithful stubs and record what
    each entity actually passes to async_dispatcher_connect."""
    print("Live registration of every entity's dispatcher targets:")

    recorded: list[tuple[str, object]] = []

    import test_protocol  # noqa: F401  (installs the base HA module stubs)

    # Overwrite the two stubs that matter with faithful versions.
    core = sys.modules["homeassistant.core"]
    core.callback = ha_callback

    disp = sys.modules.get("homeassistant.helpers.dispatcher")

    def _connect(hass, signal, target):
        recorded.append((signal, target))
        return lambda: None

    disp.async_dispatcher_connect = _connect

    # Minimal extra surface the entity modules need.
    def _mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        return m

    class _BSDC:
        HEAT = "heat"
        CONNECTIVITY = "connectivity"

    class _BinarySensorEntity:
        hass = None
        _attr_has_entity_name = True

        def async_write_ha_state(self) -> None: ...
        def async_on_remove(self, fn) -> None: ...

    _mod("homeassistant.components.binary_sensor",
         BinarySensorDeviceClass=_BSDC, BinarySensorEntity=_BinarySensorEntity)
    _mod("homeassistant.helpers.device_registry", DeviceInfo=dict)
    _mod("homeassistant.helpers.entity_platform",
         AddConfigEntryEntitiesCallback=object)

    sys.path.insert(0, os.path.join(ROOT, "custom_components"))
    import importlib
    binary_sensor = importlib.import_module("hoval_can.binary_sensor")

    class _Entry:
        entry_id = "e1"
        data = {"host": "192.0.2.10"}
        options: dict = {}

    class _Coord:
        connected = True
        electric_heater_on = False
        last_data_age = 1.0
        reconnect_count = 0
        framing_errors = 0
        last_error = None

    entities = [
        binary_sensor.HovalElectricHeaterBinarySensor(_Coord(), _Entry()),
        binary_sensor.HovalConnectivityBinarySensor(_Coord(), _Entry()),
    ]
    for ent in entities:
        asyncio.run(ent.async_added_to_hass())

    check("entities registered dispatcher targets", len(recorded) >= 3,
          f"recorded {len(recorded)}")

    bad = [
        (sig, t) for sig, t in recorded
        if infer_job_type(t) == EXECUTOR
    ]
    check(
        f"every registered target is loop-safe ({len(recorded)} targets)",
        not bad,
        "; ".join(f"{sig} -> {getattr(t, '__qualname__', t)}" for sig, t in bad),
    )

    # The specific signal from the production traceback.
    heater = [t for sig, t in recorded if sig.endswith("_electric_heater")]
    check("the electric_heater signal (crash in the 2026-09-03 log) is loop-safe",
          bool(heater) and all(infer_job_type(t) == CALLBACK for t in heater))


# ── 4. Guard the sensor base class the same way ───────────────────────────

def test_sensor_base_target_is_callback() -> None:
    print("Sensor base class:")
    path = os.path.join(COMPONENT, "sensor.py")
    tree = ast.parse(open(path, encoding="utf-8").read())

    base = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.ClassDef) and n.name == "HovalBaseEntity"),
        None,
    )
    check("HovalBaseEntity exists", base is not None)
    if base is None:
        return

    method = next(
        (n for n in base.body
         if isinstance(n, ast.FunctionDef) and n.name == "_async_signal_write_state"),
        None,
    )
    check("HovalBaseEntity._async_signal_write_state exists", method is not None)
    if method is None:
        return

    decorators = {
        d.id if isinstance(d, ast.Name) else getattr(d, "attr", "")
        for d in method.decorator_list
    }
    check("it is decorated with @callback", "callback" in decorators,
          f"decorators={decorators or '{}'}")


def main() -> int:
    test_inference_model()
    test_no_lambda_dispatcher_targets()
    test_sensor_base_target_is_callback()
    test_registered_targets_are_loop_safe()
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
