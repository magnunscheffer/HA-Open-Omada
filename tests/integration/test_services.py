import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.open_omada.const import DOMAIN

from .conftest import SITE


async def _setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize("service", ["reconnect_client", "block_client", "unblock_client"])
async def test_client_actions(hass: HomeAssistant, fake_client, config_entry, service) -> None:
    await _setup(hass, config_entry)
    await hass.services.async_call(DOMAIN, service, {"mac": "11:22:33:44:55:66"}, blocking=True)
    assert fake_client.calls == [(service.split("_")[0], SITE, "11-22-33-44-55-66")]


async def test_client_not_found(hass: HomeAssistant, fake_client, config_entry) -> None:
    await _setup(hass, config_entry)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "block_client", {"mac": "AA-AA-AA-AA-AA-AA"}, blocking=True)
    assert fake_client.calls == []


async def test_invalid_mac(hass: HomeAssistant, fake_client, config_entry) -> None:
    await _setup(hass, config_entry)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "reconnect_client", {"mac": "not-a-mac"}, blocking=True)


async def test_rename_client(hass: HomeAssistant, fake_client, config_entry) -> None:
    await _setup(hass, config_entry)
    await hass.services.async_call(
        DOMAIN, "rename_client", {"mac": "11-22-33-44-55-66", "name": "  Living room TV "}, blocking=True
    )
    assert fake_client.calls == [("rename", SITE, "11-22-33-44-55-66", "Living room TV")]
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "rename_client", {"mac": "11-22-33-44-55-66", "name": ""},
                                       blocking=True)
