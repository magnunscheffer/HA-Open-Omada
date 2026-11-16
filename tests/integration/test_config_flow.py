from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.open_omada.const import DOMAIN

from .conftest import ENTRY_DATA, OID


async def test_user_flow_creates_entry(hass: HomeAssistant, fake_client) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ENTRY_DATA, "url": ENTRY_DATA["url"] + "/"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == OID
    assert result["data"]["url"] == ENTRY_DATA["url"]


async def test_user_flow_invalid_auth_then_recover(hass: HomeAssistant, fake_client) -> None:
    fake_client.fail_auth = True
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["errors"] == {"base": "invalid_auth"}
    fake_client.fail_auth = False
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_duplicate_controller_aborts(hass: HomeAssistant, fake_client, config_entry) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(hass: HomeAssistant, fake_client, config_entry) -> None:
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"client_id": "cid", "client_secret": "new"})
    await hass.async_block_till_done()  # entry reloads after reauth
    assert result["reason"] == "reauth_successful"
    assert config_entry.data["client_secret"] == "new"
