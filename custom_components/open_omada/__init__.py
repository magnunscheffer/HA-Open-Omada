"""Open Omada: TP-Link Omada SDN via the official Open API."""

from __future__ import annotations

import hashlib
from pathlib import Path

from homeassistant.const import CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .api import OmadaAuthError, OmadaClient, OmadaError
from .const import CARD_FILE, CARD_URL, CONF_CLIENT_ID, CONF_CLIENT_SECRET, DOMAIN, LOGGER, PLATFORMS
from .coordinator import OmadaConfigEntry, OmadaCoordinator
from .entity import controller_device_info
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register integration-wide actions and the bundled dashboard card."""
    async_setup_services(hass)
    await _async_register_card(hass)
    return True


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve www/open-omada-top-wan-card.js and load it in every dashboard (no manual resource)."""
    if hass.http is None or "frontend" not in hass.config.components:
        return  # e.g. headless tests
    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig

    path = Path(__file__).parent / "www" / CARD_FILE
    # Cache-bust on content, not on the version: browsers keep the file for a long time.
    digest = hashlib.sha256(await hass.async_add_executor_job(path.read_bytes)).hexdigest()[:12]
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(path), True)])
    add_extra_js_url(hass, f"{CARD_URL}?v={digest}")


def build_client(hass: HomeAssistant, data: dict) -> OmadaClient:
    """Create an API client that reuses Home Assistant's aiohttp session."""
    verify = data.get(CONF_VERIFY_SSL, True)
    return OmadaClient(
        async_get_clientsession(hass, verify_ssl=verify),
        data[CONF_URL],
        data[CONF_CLIENT_ID],
        data[CONF_CLIENT_SECRET],
        verify_ssl=verify,
    )


async def async_setup_entry(hass: HomeAssistant, entry: OmadaConfigEntry) -> bool:
    """Set up Open Omada from a config entry."""
    client = build_client(hass, dict(entry.data))
    try:
        await client.get_controller_info()
        await client.authenticate()
    except OmadaAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="auth_failed"
        ) from err
    except OmadaError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err

    coordinator = OmadaCoordinator(hass, entry, client)
    await coordinator.async_load_port_profiles()
    await coordinator.async_config_entry_first_refresh()
    # Register the controller (hub) first so child devices can point at it.
    controller = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **controller_device_info(coordinator)
    )
    coordinator.controller_device_id = controller.id
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _remove_stale_entities(hass, entry, coordinator)
    return True


def _remove_stale_entities(hass: HomeAssistant, entry: OmadaConfigEntry, coordinator: OmadaCoordinator) -> None:
    """Drop registry entries the integration no longer provides (e.g. a port without PoE).

    Only for entities of the controller or of devices that are online and were read
    without errors, so a device that is temporarily offline keeps its entities.
    """
    registry = er.async_get(hass)
    snap = coordinator.data
    clean_sites = {s.site.id for s in snap.sites if not s.errors}
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        uid = reg_entry.unique_id
        if uid in coordinator.provided_unique_ids:
            continue
        owner = uid.split("_", 1)[0]
        if owner == snap.omadac_id:
            safe = all(not s.errors for s in snap.sites)
        else:
            ref = coordinator.devices.get(owner)
            safe = bool(ref and ref.node.device.online and ref.site.site.id in clean_sites)
        if safe:
            LOGGER.info("Removing entity %s no longer provided by Omada", reg_entry.entity_id)
            registry.async_remove(reg_entry.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: OmadaConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_flush_stores()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: OmadaConfigEntry) -> None:
    """Delete the stored port profiles when the integration is removed."""
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.port_profiles").async_remove()
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.poe_override").async_remove()


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: OmadaConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow manual removal of devices that the controller no longer reports."""
    coordinator = entry.runtime_data
    known = {coordinator.data.omadac_id} | set(coordinator.devices)
    return not any(i[0] == DOMAIN and i[1] in known for i in device.identifiers)
