"""Actions (services): reconnect, block and unblock Omada clients."""

from __future__ import annotations

import re

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_MAC
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .api import OmadaError
from .const import DOMAIN
from .coordinator import OmadaConfigEntry

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
SERVICE_RECONNECT = "reconnect_client"
SERVICE_BLOCK = "block_client"
SERVICE_UNBLOCK = "unblock_client"

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")


def _mac(value: str) -> str:
    value = cv.string(value).strip()
    if not _MAC_RE.match(value):
        raise vol.Invalid("invalid MAC address")
    return value.replace(":", "-").upper()


SCHEMA = vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string, vol.Required(CONF_MAC): _mac})


def _entry(hass: HomeAssistant, call: ServiceCall) -> OmadaConfigEntry:
    entries = [e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]
    if entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID):
        entries = [e for e in entries if e.entry_id == entry_id]
    if len(entries) != 1:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="select_controller")
    return entries[0]


async def _handle(hass: HomeAssistant, call: ServiceCall) -> None:
    client = _entry(hass, call).runtime_data.client
    mac = call.data[CONF_MAC]
    try:
        found = await client.find_client(mac)
        if found is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="client_not_found", translation_placeholders={"mac": mac}
            )
        site_id = found[0]
        action = {
            SERVICE_RECONNECT: client.reconnect_client,
            SERVICE_BLOCK: client.block_client,
            SERVICE_UNBLOCK: client.unblock_client,
        }[call.service]
        await action(site_id, mac)
    except OmadaError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="action_failed", translation_placeholders={"error": str(err)}
        ) from err


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration actions (once, in async_setup)."""

    async def handler(call: ServiceCall) -> None:
        await _handle(hass, call)

    for name in (SERVICE_RECONNECT, SERVICE_BLOCK, SERVICE_UNBLOCK):
        hass.services.async_register(DOMAIN, name, handler, schema=SCHEMA)
