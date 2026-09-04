"""Config-flow, options-flow and entry-lifecycle tests.

Two layers:

1. Functional tests of the flow logic against lightweight Home Assistant
   stubs — host normalisation, port validation, connection failure,
   duplicate detection, and options round-tripping.

2. Contract tests that assert the config-entry lifecycle matches what
   Home Assistant 2026.12 requires. From HA 2026.6 it is deprecated, and
   from 2026.12 an error, to combine a config-entry update listener with a
   reloading config-flow helper. This integration must therefore:
       * not call entry.add_update_listener()
       * pass reload_on_update=False to _abort_if_unique_id_configured()
       * subclass OptionsFlowWithReload
       * store runtime state on entry.runtime_data, not hass.data

   These are checked structurally (AST) so they cannot silently regress when
   someone reintroduces the old pattern.

    python3 tests/test_config_flow.py       # exit code 0 == all pass
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


def _tree(fname: str) -> ast.AST:
    return ast.parse(open(os.path.join(COMPONENT, fname), encoding="utf-8").read())


def _calls(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            got = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if got == name:
                yield node


# ── Layer 2: lifecycle contract (HA 2026.12) ──────────────────────────────

def test_no_update_listener() -> None:
    print("Config-entry lifecycle (HA 2026.12 deprecation):")
    init = _tree("__init__.py")
    listeners = list(_calls(init, "add_update_listener"))
    check("__init__.py registers no config-entry update listener",
          not listeners,
          f"{len(listeners)} call(s) found")

    src = open(os.path.join(COMPONENT, "__init__.py"), encoding="utf-8").read()
    check("the manual _async_reload_entry helper is gone",
          "_async_reload_entry" not in src)


def test_abort_helper_does_not_reload() -> None:
    cf = _tree("config_flow.py")
    aborts = list(_calls(cf, "_abort_if_unique_id_configured"))
    check("config flow calls _abort_if_unique_id_configured", len(aborts) == 1,
          f"found {len(aborts)}")
    if not aborts:
        return
    kw = {k.arg: k.value for k in aborts[0].keywords}
    ok = (
        "reload_on_update" in kw
        and isinstance(kw["reload_on_update"], ast.Constant)
        and kw["reload_on_update"].value is False
    )
    check("it passes reload_on_update=False", ok,
          f"keywords={sorted(kw)}")


def test_options_flow_base_class() -> None:
    cf = _tree("config_flow.py")
    cls = next(
        (n for n in ast.walk(cf)
         if isinstance(n, ast.ClassDef) and n.name == "HovalCANOptionsFlow"),
        None,
    )
    check("HovalCANOptionsFlow exists", cls is not None)
    if cls is None:
        return
    bases = {getattr(b, "id", getattr(b, "attr", "")) for b in cls.bases}
    check("it subclasses OptionsFlowWithReload",
          "OptionsFlowWithReload" in bases, f"bases={bases}")
    check("it does not subclass the plain OptionsFlow",
          "OptionsFlow" not in bases, f"bases={bases}")

    src = open(os.path.join(COMPONENT, "config_flow.py"), encoding="utf-8").read()
    check("it does not keep a private _config_entry reference",
          "_config_entry" not in src)
    check("it reads options via self.config_entry",
          "self.config_entry.options" in src)


def test_runtime_data_migration() -> None:
    print("ICS runtime-data rule:")
    for fname in ("__init__.py", "sensor.py", "binary_sensor.py", "diagnostics.py"):
        src = open(os.path.join(COMPONENT, fname), encoding="utf-8").read()
        code = "\n".join(
            line for line in src.splitlines() if not line.lstrip().startswith("#")
        )
        check(f"{fname} keeps no runtime state in hass.data",
              "hass.data" not in code)

    init = open(os.path.join(COMPONENT, "__init__.py"), encoding="utf-8").read()
    check("__init__.py assigns entry.runtime_data",
          "entry.runtime_data = coordinator" in init)

    diag = open(os.path.join(COMPONENT, "diagnostics.py"), encoding="utf-8").read()
    check("diagnostics reads runtime_data defensively (entry may be unloaded)",
          'getattr(entry, "runtime_data", None)' in diag)


def test_manifest() -> None:
    print("Manifest:")
    import json
    m = json.load(open(os.path.join(COMPONENT, "manifest.json"), encoding="utf-8"))
    check("version is 0.4.0", m.get("version") == "0.4.0", m.get("version"))
    check("integration_type is declared", m.get("integration_type") == "device",
          m.get("integration_type"))
    keys = list(m)
    check("key order satisfies hassfest (domain, name, then alphabetical)",
          keys[:2] == ["domain", "name"] and keys[2:] == sorted(keys[2:]),
          str(keys))

    hacs = json.load(open(os.path.join(ROOT, "hacs.json"), encoding="utf-8"))
    floor = hacs.get("homeassistant", "0")
    # OptionsFlowWithReload landed in 2025.7; the declared floor must be at
    # least that or HACS will offer this release to cores that cannot run it.
    major, minor = (int(x) for x in floor.split(".")[:2])
    check("hacs.json declares a HA floor that supports OptionsFlowWithReload",
          (major, minor) >= (2025, 7), floor)


def test_no_deprecated_imports() -> None:
    print("Import paths:")
    for fname in ("sensor.py", "binary_sensor.py"):
        src = open(os.path.join(COMPONENT, fname), encoding="utf-8").read()
        check(f"{fname} imports DeviceInfo from helpers.device_registry",
              "from homeassistant.helpers.device_registry import DeviceInfo" in src)
        check(f"{fname} uses AddConfigEntryEntitiesCallback",
              "AddConfigEntryEntitiesCallback" in src
              and "import AddEntitiesCallback" not in src)


# ── Layer 1: functional flow tests ────────────────────────────────────────

def _install_stubs():
    """Install the HA surface config_flow.py needs."""
    import test_protocol  # noqa: F401  (base const/core stubs)

    def _mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        return m

    class ConfigFlowResult(dict):
        pass

    class _FlowBase:
        def __init_subclass__(cls, **kw):
            kw.pop("domain", None)
            super().__init_subclass__()

        def __init__(self):
            self._unique_id = None
            self.hass = None

        async def async_set_unique_id(self, uid):
            self._unique_id = uid
            return None

        def _abort_if_unique_id_configured(self, *, reload_on_update=True):
            if self._unique_id in getattr(self, "_existing", ()):
                raise _Abort("already_configured")

        def async_create_entry(self, *, title, data, **kw):
            return ConfigFlowResult(
                type="create_entry", title=title, data=data, **kw
            )

        def async_show_form(self, *, step_id, data_schema=None, errors=None, **kw):
            return ConfigFlowResult(
                type="form", step_id=step_id,
                data_schema=data_schema, errors=errors or {},
            )

        def async_abort(self, *, reason):
            return ConfigFlowResult(type="abort", reason=reason)

    class ConfigFlow(_FlowBase):
        pass

    class OptionsFlow(_FlowBase):
        config_entry = None

    class OptionsFlowWithReload(OptionsFlow):
        """Marker base: HA reloads the entry itself after options change."""

    _mod("homeassistant.config_entries",
         ConfigEntry=object, ConfigFlow=ConfigFlow,
         ConfigFlowResult=ConfigFlowResult, OptionsFlow=OptionsFlow,
         OptionsFlowWithReload=OptionsFlowWithReload)

    return ConfigFlowResult


class _Abort(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _load_config_flow():
    _install_stubs()
    sys.path.insert(0, os.path.join(ROOT, "custom_components"))
    import importlib
    if "hoval_can.config_flow" in sys.modules:
        del sys.modules["hoval_can.config_flow"]
    return importlib.import_module("hoval_can.config_flow")


def test_flow_functional() -> None:
    print("Config flow behaviour:")
    cfmod = _load_config_flow()

    # Patch the connection probe rather than opening a real socket.
    outcome = {"ok": True}

    async def _fake_test_connection(host, port):
        outcome["last"] = (host, port)
        return outcome["ok"]

    cfmod._test_connection = _fake_test_connection

    def run(user_input, existing=()):
        flow = cfmod.HovalCANConfigFlow()
        flow._existing = existing
        try:
            return asyncio.run(flow.async_step_user(user_input))
        except _Abort as exc:
            return {"type": "abort", "reason": exc.reason}

    # 1. default port
    outcome["ok"] = True
    res = run({"host": "192.0.2.10"})
    check("valid host with default port creates an entry",
          res["type"] == "create_entry", str(res))
    check("default port 3113 is applied",
          res.get("data", {}).get("port") == 3113,
          str(res.get("data")))

    # 2. custom port
    res = run({"host": "192.0.2.10", "port": 5000})
    check("custom port is honoured",
          res.get("data", {}).get("port") == 5000, str(res.get("data")))

    # 3. whitespace stripped
    res = run({"host": "  192.0.2.10  "})
    check("surrounding whitespace is stripped from the host",
          res.get("data", {}).get("host") == "192.0.2.10",
          repr(res.get("data", {}).get("host")))

    # 4. connection failure
    outcome["ok"] = False
    res = run({"host": "192.0.2.99"})
    check("unreachable gateway re-shows the form",
          res["type"] == "form", str(res))
    check("with a cannot_connect error",
          res.get("errors", {}).get("base") == "cannot_connect",
          str(res.get("errors")))

    # 5. duplicate
    outcome["ok"] = True
    res = run({"host": "192.0.2.10"}, existing={"192.0.2.10:3113"})
    check("a duplicate host:port aborts as already_configured",
          res.get("reason") == "already_configured", str(res))

    # 6. unique id shape
    flow = cfmod.HovalCANConfigFlow()
    flow._existing = ()
    asyncio.run(flow.async_step_user({"host": "10.0.0.5", "port": 3113}))
    check("unique_id is host:port", flow._unique_id == "10.0.0.5:3113",
          flow._unique_id)

    # 7. port bounds enforced by the schema
    import voluptuous as vol
    for bad in (0, 65536, -1):
        try:
            cfmod.STEP_USER_SCHEMA({"host": "h", "port": bad})
            ok = False
        except vol.Invalid:
            ok = True
        check(f"port {bad} is rejected by the schema", ok)
    try:
        cfmod.STEP_USER_SCHEMA({"host": "h", "port": 3113})
        ok = True
    except vol.Invalid:
        ok = False
    check("port 3113 is accepted by the schema", ok)


def test_options_flow_functional() -> None:
    print("Options flow behaviour:")
    cfmod = _load_config_flow()
    from hoval_can import const

    class _Entry:
        options = {const.CONF_HEATER_POWER: 6.0, const.CONF_STANDBY_POWER: 25.0}

    flow = cfmod.HovalCANOptionsFlow()
    flow.config_entry = _Entry()

    form = asyncio.run(flow.async_step_init(None))
    check("options flow shows the init form", form["type"] == "form", str(form))

    schema = form["data_schema"]
    defaults = {
        str(k.schema): k.default() for k in schema.schema if hasattr(k, "default")
    }
    check("stored heater power is pre-filled",
          defaults.get(const.CONF_HEATER_POWER) == 6.0,
          str(defaults.get(const.CONF_HEATER_POWER)))
    check("stored standby power is pre-filled",
          defaults.get(const.CONF_STANDBY_POWER) == 25.0,
          str(defaults.get(const.CONF_STANDBY_POWER)))
    check("unset options fall back to their defaults",
          defaults.get(const.CONF_SOURCE_TEMP) == const.DEFAULT_SOURCE_TEMP_C,
          str(defaults.get(const.CONF_SOURCE_TEMP)))

    # All seven options must be present so saving one cannot drop the others.
    expected = {
        const.CONF_HEATER_POWER, const.CONF_COOLING_POWER,
        const.CONF_SOURCE_TEMP, const.CONF_APPROACH_K,
        const.CONF_BRINE_PUMP_POWER, const.CONF_HEATING_PUMP_POWER,
        const.CONF_STANDBY_POWER,
    }
    check("all seven options are in the schema",
          set(defaults) == expected,
          f"missing={expected - set(defaults)} extra={set(defaults) - expected}")

    submitted = {k: 1.0 for k in expected}
    res = asyncio.run(flow.async_step_init(submitted))
    check("submitting creates the options entry",
          res["type"] == "create_entry", str(res))
    check("every submitted option is persisted",
          res["data"] == submitted, str(res.get("data")))

    # The reload is the framework's job now.
    base_names = {c.__name__ for c in type(flow).__mro__}
    check("the handler inherits OptionsFlowWithReload (framework owns reload)",
          "OptionsFlowWithReload" in base_names, str(base_names))


def main() -> int:
    test_no_update_listener()
    test_abort_helper_does_not_reload()
    test_options_flow_base_class()
    test_runtime_data_migration()
    test_no_deprecated_imports()
    test_manifest()
    test_flow_functional()
    test_options_flow_functional()
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
