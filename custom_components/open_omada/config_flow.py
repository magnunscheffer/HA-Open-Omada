"""Config flow for Open Omada: user setup, reauth and reconfigure."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant

from .api import OmadaAuthError, OmadaConnectionError, OmadaError
from .const import CONF_CLIENT_ID, CONF_CLIENT_SECRET, DOMAIN, LOGGER

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): str,
        vol.Required(CONF_CLIENT_ID): str,
        vol.Required(CONF_CLIENT_SECRET): str,
        vol.Required(CONF_VERIFY_SSL, default=False): bool,
    }
)
REAUTH_SCHEMA = vol.Schema(
    {vol.Required(CONF_CLIENT_ID): str, vol.Required(CONF_CLIENT_SECRET): str}
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Authenticate and list sites; return controller info. Raises OmadaError."""
    from . import build_client  # local import: avoids a cycle at module load

    client = build_client(hass, data)
    info = await client.get_controller_info()
    await client.authenticate()
    await client.get_sites()
    return info


class OmadaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Open Omada."""

    VERSION = 1

    async def _try(self, data: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, str]]:
        try:
            return await validate_input(self.hass, data), {}
        except OmadaAuthError:
            return None, {"base": "invalid_auth"}
        except OmadaConnectionError:
            return None, {"base": "cannot_connect"}
        except OmadaError:
            LOGGER.exception("Unexpected Omada API error")
            return None, {"base": "unknown"}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_URL] = user_input[CONF_URL].rstrip("/")
            info, errors = await self._try(user_input)
            if info is not None:
                await self.async_set_unique_id(info["omadacId"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Omada {info.get('controllerVer', '')}".strip(), data=user_input
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            data = {**entry.data, **user_input}
            info, errors = await self._try(data)
            if info is not None:
                await self.async_set_unique_id(info["omadacId"])
                self._abort_if_unique_id_mismatch(reason="wrong_controller")
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                REAUTH_SCHEMA, {CONF_CLIENT_ID: entry.data[CONF_CLIENT_ID]}
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            user_input[CONF_URL] = user_input[CONF_URL].rstrip("/")
            info, errors = await self._try(user_input)
            if info is not None:
                await self.async_set_unique_id(info["omadacId"])
                self._abort_if_unique_id_mismatch(reason="wrong_controller")
                return self.async_update_reload_and_abort(entry, data=user_input)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input or entry.data),
            errors=errors,
        )
