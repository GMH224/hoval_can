"""Config flow and options flow for Hoval CAN."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback

from .const import (
    CONF_HEATER_POWER, DEFAULT_HEATER_POWER_KW,
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
        return HovalCANOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input.get(CONF_PORT, DEFAULT_PORT)
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()
            if await _test_connection(host, port):
                return self.async_create_entry(
                    title=f"Hoval CAN ({host})",
                    data={CONF_HOST: host, CONF_PORT: port},
                )
            errors["base"] = "cannot_connect"
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors,
        )


class HovalCANOptionsFlow(OptionsFlow):
    """Options: electric heater rated power.

    COP is calculated automatically from live sensor data and does not
    appear here.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = float(
            self._config_entry.options.get(CONF_HEATER_POWER, DEFAULT_HEATER_POWER_KW)
        )
        schema = vol.Schema({
            vol.Required(CONF_HEATER_POWER, default=current): vol.All(
                vol.Coerce(float),
                vol.Range(min=HEATER_POWER_MIN, max=HEATER_POWER_MAX),
            ),
        })
        return self.async_show_form(step_id="init", data_schema=schema)
