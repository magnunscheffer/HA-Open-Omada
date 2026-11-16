"""Config flow for Open Omada: user setup, reauth and reconfigure."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import OmadaAuthError, OmadaConnectionError, OmadaError
from .api.wanprobe import DEFAULT_PROBES, parse_targets
from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_CONFIRM_INTERVAL,
    CONF_CONFIRM_TESTS,
    CONF_COOLDOWN,
    CONF_FAILBACK,
    CONF_INTERVAL,
    CONF_LATENCY,
    CONF_PROBES_WAN1,
    CONF_PROBES_WAN2,
    CONF_WAN_CONTROL,
    DOMAIN,
    LOGGER,
    WAN_CONTROL_DEFAULTS,
)

PLACEHOLDERS = {"example_url": "https://omada.local:8043"}

_SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_URL = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): _URL,
        vol.Required(CONF_CLIENT_ID): str,
        vol.Required(CONF_CLIENT_SECRET): _SECRET,
        vol.Required(CONF_VERIFY_SSL, default=True): bool,
    }
)
# Reconfigure never sends the stored secret back to the browser; empty = keep it.
RECONFIGURE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): _URL,
        vol.Required(CONF_CLIENT_ID): str,
        vol.Optional(CONF_CLIENT_SECRET): _SECRET,
        vol.Required(CONF_VERIFY_SSL, default=True): bool,
    }
)
REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_CLIENT_ID): str, vol.Required(CONF_CLIENT_SECRET): _SECRET})


def _normalize_url(data: dict[str, Any]) -> str | None:
    """Normalise the controller URL in place; return an error key if it is not acceptable.

    Accepted: ``https://host[:port]`` (DNS name, IPv4 or [IPv6]) with an optional trailing "/".
    Rejected: other schemes, embedded user/password, a path, query or fragment.
    """
    url = str(data[CONF_URL]).strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        return "https_required"
    try:
        port = parsed.port  # raises ValueError on an invalid port
    except ValueError:
        return "invalid_url"
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return "invalid_url"
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    data[CONF_URL] = f"https://{host}" + (f":{port}" if port else "")
    return None


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Authenticate and list sites; return controller info. Raises OmadaError."""
    from . import build_client  # local import: avoids a cycle at module load

    client = build_client(hass, data)
    info = await client.get_controller_info()
    await client.authenticate()
    await client.get_sites()
    return info


def _num(low: float, high: float, step: float = 1) -> NumberSelector:
    return NumberSelector(NumberSelectorConfig(min=low, max=high, step=step, mode=NumberSelectorMode.BOX))


class OmadaOptionsFlow(OptionsFlow):
    """Enhanced WAN Control settings."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        opts = {**WAN_CONTROL_DEFAULTS,
                CONF_PROBES_WAN1: ", ".join(DEFAULT_PROBES[1]), CONF_PROBES_WAN2: ", ".join(DEFAULT_PROBES[2]),
                **self.config_entry.options}
        errors: dict[str, str] = {}
        if user_input is not None:
            p1, p2 = parse_targets(user_input[CONF_PROBES_WAN1]), parse_targets(user_input[CONF_PROBES_WAN2])
            if user_input[CONF_WAN_CONTROL] and (not p1 or not p2):
                errors["base"] = "probes_required"
            elif set(p1) & set(p2):
                errors["base"] = "probes_overlap"  # one IP cannot be pinned to both WANs
            else:
                data = {**user_input, CONF_PROBES_WAN1: ", ".join(p1), CONF_PROBES_WAN2: ", ".join(p2)}
                ctl = getattr(getattr(self.config_entry, "runtime_data", None), "wan_control", None)
                if ctl and opts[CONF_WAN_CONTROL] and not user_input[CONF_WAN_CONTROL]:
                    try:  # turned off: remove the managed probe objects from the controller
                        await ctl.async_remove_objects()
                    except OmadaError:
                        LOGGER.exception("Could not remove the Enhanced WAN Control objects")
                return self.async_create_entry(data=data)
            opts = {**opts, **user_input}
        schema = vol.Schema({
            vol.Required(CONF_WAN_CONTROL, default=opts[CONF_WAN_CONTROL]): bool,
            vol.Required(CONF_PROBES_WAN1, default=opts[CONF_PROBES_WAN1]): str,
            vol.Required(CONF_PROBES_WAN2, default=opts[CONF_PROBES_WAN2]): str,
            vol.Required(CONF_LATENCY, default=opts[CONF_LATENCY]): _num(20, 2000),
            vol.Required(CONF_INTERVAL, default=opts[CONF_INTERVAL]): _num(15, 600),
            vol.Required(CONF_CONFIRM_TESTS, default=opts[CONF_CONFIRM_TESTS]): _num(0, 5),
            vol.Required(CONF_CONFIRM_INTERVAL, default=opts[CONF_CONFIRM_INTERVAL]): _num(1, 60),
            vol.Required(CONF_COOLDOWN, default=opts[CONF_COOLDOWN]): _num(1, 240),
            vol.Required(CONF_FAILBACK, default=opts[CONF_FAILBACK]): _num(1, 1440),
        })
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)


class OmadaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Open Omada."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OmadaOptionsFlow:
        return OmadaOptionsFlow()

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
        if user_input is not None and (bad := _normalize_url(user_input)):
            errors = {CONF_URL: bad}
        elif user_input is not None:
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
            description_placeholders=PLACEHOLDERS,
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
            data = {**user_input}
            if not data.get(CONF_CLIENT_SECRET):
                data[CONF_CLIENT_SECRET] = entry.data[CONF_CLIENT_SECRET]
            if bad := _normalize_url(data):
                errors = {CONF_URL: bad}
            else:
                info, errors = await self._try(data)
                if info is not None:
                    await self.async_set_unique_id(info["omadacId"])
                    self._abort_if_unique_id_mismatch(reason="wrong_controller")
                    return self.async_update_reload_and_abort(entry, data=data)
        shown = {k: v for k, v in (user_input or entry.data).items() if k != CONF_CLIENT_SECRET}
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(RECONFIGURE_SCHEMA, shown),
            errors=errors,
            description_placeholders=PLACEHOLDERS,
        )
