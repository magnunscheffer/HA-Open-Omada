import pytest
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


async def test_http_url_rejected(hass: HomeAssistant, fake_client) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ENTRY_DATA, "url": "http://omada.test:8088"})
    assert result["errors"] == {"url": "https_required"}


async def test_reconfigure_keeps_secret_when_empty(hass: HomeAssistant, fake_client, config_entry) -> None:
    result = await config_entry.start_reconfigure_flow(hass)
    schema_keys = {str(k): k for k in result["data_schema"].schema}
    assert "suggested_value" not in (schema_keys["client_secret"].description or {})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": ENTRY_DATA["url"], "client_id": "cid", "verify_ssl": False})
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data["client_secret"] == "sec"


@pytest.mark.parametrize(
    ("url", "error"),
    [
        ("http://omada.test:8088", "https_required"),
        ("ftp://omada.test", "https_required"),
        ("https://user:pass@omada.test:8043", "invalid_url"),
        ("https://omada.test:8043/omada", "invalid_url"),
        ("https://omada.test:8043/?x=1", "invalid_url"),
        ("https://omada.test:8043/#frag", "invalid_url"),
        ("https://omada.test:99999", "invalid_url"),
        ("https://", "invalid_url"),
    ],
)
async def test_url_validation_rejects(hass: HomeAssistant, fake_client, url, error) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**ENTRY_DATA, "url": url})
    assert result["errors"] == {"url": error}


@pytest.mark.parametrize(
    ("url", "stored"),
    [
        ("https://Omada.Test:8043/", "https://omada.test:8043"),
        ("  https://omada.test  ", "https://omada.test"),
        ("https://192.0.2.10:8043", "https://192.0.2.10:8043"),
        ("https://[2001:db8::1]:8043", "https://[2001:db8::1]:8043"),
    ],
)
async def test_url_validation_accepts_and_normalises(hass: HomeAssistant, fake_client, url, stored) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**ENTRY_DATA, "url": url})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["url"] == stored
