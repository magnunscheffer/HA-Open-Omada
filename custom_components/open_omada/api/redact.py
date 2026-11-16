"""Redaction shared by HA diagnostics and the CLI (`--redact`).

Device and host names (including FQDNs) are kept on purpose: they are needed to diagnose
problems. Secrets, MAC/IP addresses, serial numbers and locations are replaced.
"""

from __future__ import annotations

from typing import Any

REDACTED = "**REDACTED**"

REDACT_KEYS: frozenset[str] = frozenset({
    # credentials / identifiers
    "client_id", "client_secret", "omadacId", "omadac_id", "accessToken", "refreshToken",
    # hardware identifiers
    "mac", "uplinkDeviceMac", "switchMac", "apMac", "gatewayMac", "serverMac", "sn", "serial",
    "serial_number",
    # addresses
    "ip", "ip2", "ipv6", "ipv6List", "publicIp", "sitePublicIp", "gateway", "gateway2",
    "priDns", "sndDns", "priDns2", "sndDns2", "wanPortIpv4Config", "wanPortIpv6Config",
    "forwardIp", "wanIps", "limitedAddresses", "dhcp",
    # location
    "longitude", "latitude", "address",
})


def redact(data: Any, keys: frozenset[str] = REDACT_KEYS) -> Any:
    """Return a copy of ``data`` with the values of sensitive keys replaced (recursively)."""
    if isinstance(data, dict):
        return {k: (REDACTED if k in keys and v not in (None, "", [], {}) else redact(v, keys))
                for k, v in data.items()}
    if isinstance(data, list):
        return [redact(v, keys) for v in data]
    return data
