import json
import re

from homeassistant.core import HomeAssistant

from custom_components.open_omada.diagnostics import async_get_config_entry_diagnostics

from .conftest import ENTRY_DATA, GW, SW

MAC_RE = re.compile(r"(?i)\b[0-9a-f]{2}(?:[-:][0-9a-f]{2}){5}\b")
SECRET_VALUES = ("192.168.77.10", "203.0.113.7", "8.8.4.4", "10.0.0.5", "SNSECRET123",
                 ENTRY_DATA["client_secret"], ENTRY_DATA["client_id"])


async def test_diagnostics_redact_identifiers_but_keep_names(hass: HomeAssistant, fake_client, config_entry) -> None:
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    text = json.dumps(diag)
    assert not MAC_RE.findall(text), MAC_RE.findall(text)[:5]
    for value in SECRET_VALUES:
        assert f'"{value}"' not in text, value
    assert GW not in text and SW not in text
    # Owner decision: device/host names and the controller FQDN stay visible.
    assert "fw1.lab.example" in text and "sw1" in text
    assert diag["entry"]["url"] == ENTRY_DATA["url"]
