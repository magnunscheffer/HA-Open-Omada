"""Diagnostics with secrets and network identifiers redacted."""

from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_CLIENT_ID, CONF_CLIENT_SECRET
from .coordinator import OmadaConfigEntry

TO_REDACT = {
    CONF_CLIENT_ID, CONF_CLIENT_SECRET, "omadacId", "omadac_id", "mac", "sn", "serial",
    "ip", "publicIp", "sitePublicIp", "ipv6", "ipv6List", "wanPortIpv4Config",
    "wanPortIpv6Config", "gateway", "priDns", "sndDns", "uplinkDeviceMac", "longitude", "latitude",
    "url", "forwardIp", "wanIps", "limitedAddresses", "ip2", "gateway2", "priDns2", "sndDns2",
    "serial_number", "dhcp",
}


def _plain(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj):
        return {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, list):
        return [_plain(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    return obj


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: OmadaConfigEntry
) -> dict[str, Any]:
    snap = entry.runtime_data.data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "snapshot": async_redact_data(_plain(snap), TO_REDACT),
    }
