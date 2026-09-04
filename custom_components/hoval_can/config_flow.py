"""Config flow and options flow for Hoval CAN."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback

from .const import (
    CONF_HEATER_POWER, DEFAULT_HEATER_POWER_KW,
    CONF_COOLING_POWER, DEFAULT_COOLING_POWER_W,
    COOLING_POWER_MAX, COOLING_POWER_MIN,
    CONF_SOURCE_TEMP, DEFAULT_SOURCE_TEMP_C,
    SOURCE_TEMP_MAX, SOURCE_TEMP_MIN,
    CONF_APPROACH_K, DEFAULT_APPROACH_K_C,
    APPROACH_K_MAX, APPROACH_K_MIN,
    CONF_BRINE_PUMP_POWER, DEFAULT_BRINE_PUMP_POWER_W,
    BRINE_PUMP_POWER_MAX, BRINE_PUMP_POWER_MIN,
    CONF_HEATING_PUMP_POWER, DEFAULT_HEATING_PUMP_POWER_W,
    HEATING_PUMP_POWER_MAX, HEATING_PUMP_POWER_MIN,
    CONF_STANDBY_POWER, DEFAULT_STANDBY_POWER_W,
    STANDBY_POWER_MAX, STANDBY_POWER_MIN,
    DEFAULT_PORT, DOMAIN,
    HEATER_POWER_MAX, HEATER_POWER_MIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema({
    vol.Required(CONF_HOST): str,
    vol.Optional(CONF_PORT, default=DEFAULT_PORT): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=65535)
    ),
})


async def _test_connection(host: str, port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=8
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception:  # noqa: BLE001
        return False


class HovalCANConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup: IP address and port."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HovalCANOptionsFlow:
        # HA injects the entry; OptionsFlow.config_entry is a read-only
        # property, so the handler must not be constructed with it.
        return HovalCANOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input.get(CONF_PORT, DEFAULT_PORT)
            await self.async_set_unique_id(f"{host}:{port}")
            # reload_on_update=False is required from HA 2026.12: combining a
            # reloading config-flow helper with a config-entry update listener
            # is an error. The listener is gone (see __init__.py) and reloads
            # on option changes are owned by OptionsFlowWithReload below.
            self._abort_if_unique_id_configured(reload_on_update=False)
            if await _test_connection(host, port):
                return self.async_create_entry(
                    title=f"Hoval CAN ({host})",
                    data={CONF_HOST: host, CONF_PORT: port},
                )
            errors["base"] = "cannot_connect"
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors,
        )


class HovalCANOptionsFlow(OptionsFlowWithReload):
    """Options: electric heater rated power, passive-cooling pump power,
    ground-loop source temperature, COP approach temperature k (v0.3.1),
    brine/heating pump power, and standby power.

    COP is calculated automatically from live sensor data — only the source
    (ground-loop) temperature it needs is configurable here, since no CAN
    datapoint reports it on this installation.

    Subclasses OptionsFlowWithReload: Home Assistant reloads the entry
    itself once the options change, so the integration must not also register
    a config-entry update listener (that combination is an error from
    HA 2026.12).
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        opts = self.config_entry.options
        current_heater = float(
            opts.get(CONF_HEATER_POWER, DEFAULT_HEATER_POWER_KW)
        )
        current_cooling = float(
            opts.get(CONF_COOLING_POWER, DEFAULT_COOLING_POWER_W)
        )
        current_source_temp = float(
            opts.get(CONF_SOURCE_TEMP, DEFAULT_SOURCE_TEMP_C)
        )
        current_approach_k = float(
            opts.get(CONF_APPROACH_K, DEFAULT_APPROACH_K_C)
        )
        current_brine_pump = float(
            opts.get(CONF_BRINE_PUMP_POWER, DEFAULT_BRINE_PUMP_POWER_W)
        )
        current_heating_pump = float(
            opts.get(CONF_HEATING_PUMP_POWER, DEFAULT_HEATING_PUMP_POWER_W)
        )
        current_standby = float(
            opts.get(CONF_STANDBY_POWER, DEFAULT_STANDBY_POWER_W)
        )
        # All fields are present in the schema so that saving one does not
        # drop the others (async_create_entry replaces the whole options dict).
        schema = vol.Schema({
            vol.Required(CONF_HEATER_POWER, default=current_heater): vol.All(
                vol.Coerce(float),
                vol.Range(min=HEATER_POWER_MIN, max=HEATER_POWER_MAX),
            ),
            vol.Required(CONF_COOLING_POWER, default=current_cooling): vol.All(
                vol.Coerce(float),
                vol.Range(min=COOLING_POWER_MIN, max=COOLING_POWER_MAX),
            ),
            vol.Required(CONF_SOURCE_TEMP, default=current_source_temp): vol.All(
                vol.Coerce(float),
                vol.Range(min=SOURCE_TEMP_MIN, max=SOURCE_TEMP_MAX),
            ),
            vol.Required(CONF_APPROACH_K, default=current_approach_k): vol.All(
                vol.Coerce(float),
                vol.Range(min=APPROACH_K_MIN, max=APPROACH_K_MAX),
            ),
            vol.Required(CONF_BRINE_PUMP_POWER, default=current_brine_pump): vol.All(
                vol.Coerce(float),
                vol.Range(min=BRINE_PUMP_POWER_MIN, max=BRINE_PUMP_POWER_MAX),
            ),
            vol.Required(
                CONF_HEATING_PUMP_POWER, default=current_heating_pump
            ): vol.All(
                vol.Coerce(float),
                vol.Range(min=HEATING_PUMP_POWER_MIN, max=HEATING_PUMP_POWER_MAX),
            ),
            vol.Required(CONF_STANDBY_POWER, default=current_standby): vol.All(
                vol.Coerce(float),
                vol.Range(min=STANDBY_POWER_MIN, max=STANDBY_POWER_MAX),
            ),
        })
        return self.async_show_form(step_id="init", data_schema=schema)
