"""Config flow and options flow for Hoval CAN."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback

from .const import CONF_COP, COP_MAX, COP_MIN, DEFAULT_COP, DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
    }
)


async def _test_connection(host: str, port: int) -> bool:
    """Try opening a TCP connection; return True if successful."""
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
    """Handle initial setup: IP address + port."""

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
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )


class HovalCANOptionsFlow(OptionsFlow):
    """Allow the user to change COP without removing the integration."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_cop = float(
            self._config_entry.options.get(CONF_COP, DEFAULT_COP)
        )

        schema = vol.Schema(
            {
                vol.Required(CONF_COP, default=current_cop): vol.All(
                    vol.Coerce(float),
                    vol.Range(min=COP_MIN, max=COP_MAX),
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
