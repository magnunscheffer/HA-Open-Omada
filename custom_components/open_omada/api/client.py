"""Async client for the TP-Link Omada SDN Controller Open API.

This module has no Home Assistant dependency so it can be exercised from the
CLI in ``scripts/`` and from unit tests.

Auth flow (client credentials mode):
    POST /openapi/authorize/token?grant_type=client_credentials
         body {omadacId, client_id, client_secret}
    POST /openapi/authorize/token?grant_type=refresh_token&refresh_token=RT
         body {client_id, client_secret}
    Header on every call:  Authorization: AccessToken=<accessToken>

Access tokens last 2h and refresh tokens 14 days. See docs/api-reference.md.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from .exceptions import (
    OmadaApiError,
    OmadaAuthError,
    OmadaConnectionError,
    OmadaError,
    OmadaNotSupportedError,
)

_LOGGER = logging.getLogger(__name__)

# Error codes documented in the Open API spec.
ERR_TOKEN_EXPIRED = -44112
ERR_TOKEN_INVALID = -44113
ERR_REFRESH_EXPIRED = -44114
ERR_BAD_CLIENT = -44106
ERR_AUTH_FAILED = -44116
ERR_NOT_SUPPORTED = -44205
_REAUTH_CODES = {ERR_TOKEN_EXPIRED, ERR_TOKEN_INVALID}
_FATAL_AUTH_CODES = {ERR_BAD_CLIENT, ERR_AUTH_FAILED}

DEFAULT_TIMEOUT = 30
MAX_PAGE_SIZE = 1000
# Renew the token a bit before it expires.
TOKEN_SAFETY_MARGIN = 120


def _safe_error(err: BaseException) -> str:
    """Describe a transport error without its text: aiohttp messages can embed the request
    URL, and the token endpoint carries the refresh token in the query string."""
    name = type(err).__name__
    host, port = getattr(err, "host", None), getattr(err, "port", None)
    if isinstance(host, str) and host:
        return f"{name} ({host}:{port})" if port else f"{name} ({host})"
    return name


def format_mac(mac: str) -> str:
    """Return a MAC in the AA-BB-CC-DD-EE-FF form expected by the API."""
    return mac.replace(":", "-").upper()


class OmadaClient:
    """Minimal, polite async client for the Omada Open API.

    Requests are serialised through a semaphore (``max_concurrency``) because
    the controller is easy to overload.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        client_id: str,
        client_secret: str,
        *,
        omadac_id: str | None = None,
        verify_ssl: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
        max_concurrency: int = 2,
    ) -> None:
        self._session = session
        self._url = url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._omadac_id = omadac_id
        self._ssl: bool | None = None if verify_ssl else False
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._sem = asyncio.Semaphore(max_concurrency)
        self._auth_lock = asyncio.Lock()
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0
        self.controller_info: dict[str, Any] = {}

    # ------------------------------------------------------------------ props
    @property
    def omadac_id(self) -> str:
        if not self._omadac_id:
            raise OmadaError("omadacId unknown; call get_controller_info() first")
        return self._omadac_id

    @property
    def url(self) -> str:
        return self._url

    # -------------------------------------------------------------- low level
    async def _raw(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"AccessToken={self._access_token}"
        try:
            async with self._sem:
                async with self._session.request(
                    method,
                    f"{self._url}{path}",
                    params=params,
                    json=json,
                    headers=headers,
                    ssl=self._ssl,
                    timeout=self._timeout,
                    allow_redirects=False,
                ) as resp:
                    # Never use raise_for_status()/repr(err): aiohttp includes the
                    # request headers (our access token) in those messages.
                    if resp.status in (401, 403):
                        raise OmadaAuthError(f"HTTP {resp.status} on {method} {path}")
                    if resp.status >= 400:
                        raise OmadaApiError(resp.status, f"HTTP {resp.status} {resp.reason}", f"{method} {path}")
                    try:
                        body = await resp.json(content_type=None)
                    except ValueError:
                        raise OmadaApiError(resp.status, "response is not JSON", f"{method} {path}") from None
        except aiohttp.ClientResponseError as err:
            # str(err) would include the request URL, whose query may carry the refresh token.
            raise OmadaConnectionError(f"{method} {path}: {type(err).__name__} {err.status}") from None
        except (TimeoutError, aiohttp.ClientError) as err:
            raise OmadaConnectionError(f"{method} {path}: {_safe_error(err)}") from None
        if not isinstance(body, dict):
            raise OmadaApiError(-1, "unexpected response body", f"{method} {path}")
        return body

    @staticmethod
    def _check(body: dict[str, Any], path: str) -> Any:
        code = body.get("errorCode", 0)
        if code == 0:
            return body.get("result")
        msg = body.get("msg", "")
        if code in _FATAL_AUTH_CODES or code == ERR_REFRESH_EXPIRED:
            raise OmadaAuthError(f"{code}: {msg}")
        if code == ERR_NOT_SUPPORTED:
            raise OmadaNotSupportedError(code, msg, path)
        raise OmadaApiError(code, msg, path)

    # ------------------------------------------------------------------- auth
    async def get_controller_info(self) -> dict[str, Any]:
        """Public endpoint: version, omadacId, etc. No auth required."""
        body = await self._raw("GET", "/api/info", auth=False)
        info = self._check(body, "/api/info") or {}
        self.controller_info = info
        self._omadac_id = self._omadac_id or info.get("omadacId")
        return info

    async def _store_token(self, result: Any) -> None:
        if not isinstance(result, dict) or not result.get("accessToken"):
            raise OmadaAuthError("token endpoint returned no access token")
        self._access_token = result["accessToken"]
        self._refresh_token = result.get("refreshToken")
        self._expires_at = time.monotonic() + int(result.get("expiresIn", 7200))

    async def authenticate(self) -> None:
        """Obtain a fresh token using the client credentials grant."""
        if not self._omadac_id:
            await self.get_controller_info()
        body = await self._raw(
            "POST",
            "/openapi/authorize/token",
            params={"grant_type": "client_credentials"},
            json={
                "omadacId": self._omadac_id,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            auth=False,
        )
        await self._store_token(self._check(body, "/openapi/authorize/token"))
        _LOGGER.debug("Authenticated to Omada controller %s", self._url)

    async def _refresh(self) -> None:
        if not self._refresh_token:
            await self.authenticate()
            return
        try:
            body = await self._raw(
                "POST",
                "/openapi/authorize/token",
                params={"grant_type": "refresh_token", "refresh_token": self._refresh_token},
                json={"client_id": self._client_id, "client_secret": self._client_secret},
                auth=False,
            )
            await self._store_token(self._check(body, "/openapi/authorize/token"))
        except (OmadaAuthError, OmadaApiError):
            await self.authenticate()

    async def _ensure_token(self, force: bool = False, rejected: str | None = None) -> None:
        """Make sure a valid token exists. ``rejected`` = token the controller just refused:
        if another request already replaced it, do not refresh again."""
        async with self._auth_lock:
            if self._access_token is None:
                await self.authenticate()
            elif force and rejected is not None and rejected != self._access_token:
                return
            elif force or time.monotonic() > self._expires_at - TOKEN_SAFETY_MARGIN:
                await self._refresh()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        """Authenticated call. ``path`` may contain ``{omadacId}``."""
        if "{omadacId}" in path:
            if not self._omadac_id:
                await self.get_controller_info()
            path = path.replace("{omadacId}", self.omadac_id)
        await self._ensure_token()
        used = self._access_token
        body = await self._raw(method, path, params=params, json=json)
        if body.get("errorCode") in _REAUTH_CODES:
            await self._ensure_token(force=True, rejected=used)
            body = await self._raw(method, path, params=params, json=json)
        return self._check(body, path)

    async def get_paged(self, path: str, params: dict[str, Any] | None = None) -> list[dict]:
        """Fetch every page of a grid endpoint and return the merged ``data``."""
        out: list[dict] = []
        page = 1
        while True:
            q = {"page": page, "pageSize": MAX_PAGE_SIZE, **(params or {})}
            result = await self.request("GET", path, params=q) or {}
            if isinstance(result, list):  # some endpoints are not paginated
                return result
            data = result.get("data") or []
            out.extend(data)
            total = result.get("totalRows", len(out))
            if not data or len(out) >= total:
                return out
            page += 1

    # ------------------------------------------------------------- read APIs
    def _site(self, site_id: str, suffix: str = "", version: int = 1) -> str:
        return f"/openapi/v{version}/{{omadacId}}/sites/{site_id}{suffix}"

    async def get_sites(self) -> list[dict]:
        return await self.get_paged("/openapi/v1/{omadacId}/sites")

    async def get_devices(self, site_id: str) -> list[dict]:
        return await self.get_paged(self._site(site_id, "/devices"))

    async def get_gateway(self, site_id: str, mac: str) -> dict:
        return await self.request("GET", self._site(site_id, f"/gateways/{format_mac(mac)}"))

    async def get_gateway_wan_status(self, site_id: str, mac: str) -> list[dict]:
        return await self.request(
            "GET", self._site(site_id, f"/gateways/{format_mac(mac)}/wan-status")
        ) or []

    async def get_switch(self, site_id: str, mac: str, *, agile: bool = False) -> dict:
        """Switch overview. Agile Series (device subtype ``es``) use /switches/es/."""
        prefix = "/switches/es/" if agile else "/switches/"
        return await self.request("GET", self._site(site_id, f"{prefix}{format_mac(mac)}"))

    async def get_switch_stat(self, site_id: str, mac: str) -> dict:
        return await self.request("GET", self._site(site_id, f"/stat/switches/{format_mac(mac)}"))

    async def get_switch_ports_poe(self, site_id: str) -> list[dict]:
        """PoE capability and live power of every switch port of the site."""
        return await self.get_paged(self._site(site_id, "/switches/ports/poe-info"))

    async def get_switch_ports_overview(self, site_id: str) -> list[dict]:
        """Config summary of every switch port of the site (profile, override, VLAN mode)."""
        return await self.get_paged(self._site(site_id, "/switches/ports/overview"))

    async def set_switch_port_poe_via_override(
        self, site_id: str, mac: str, port: int, on: bool, *,
        port_name: str, profile_id: str, native_network_id: str, network_tags_setting: int,
    ) -> None:
        """Set PoE on a port that follows its profile (controller >= 6.2.10).

        PoE can only change with profile override on; PUT .../profile-override is rejected
        (-1001) on 6.3, so enable it through PATCH, keeping the port's current native
        network and tag mode. Only safe for tag mode 0/1: mode 2 (custom tagged VLANs)
        would need the VLAN lists, which the API does not return. Verified on SG2218P.
        """
        if network_tags_setting not in (0, 1):
            raise ValueError("custom VLAN tagging: refusing to enable profile override")
        await self.request(
            "PATCH",
            self._site(site_id, f"/switches/{format_mac(mac)}/ports/{port}"),
            json={"name": port_name, "profileId": profile_id, "profileOverrideEnable": True,
                  "nativeNetworkId": native_network_id, "networkTagsSetting": network_tags_setting,
                  "poe": 1 if on else 0},
        )

    async def set_switch_port_follow_profile(
        self, site_id: str, mac: str, port: int, *, port_name: str, profile_id: str
    ) -> None:
        """Turn profile override off (port follows its profile again). PoE is kept as is."""
        await self.request(
            "PATCH",
            self._site(site_id, f"/switches/{format_mac(mac)}/ports/{port}"),
            json={"name": port_name, "profileId": profile_id, "profileOverrideEnable": False},
        )

    async def get_ap(self, site_id: str, mac: str) -> dict:
        return await self.request("GET", self._site(site_id, f"/aps/{format_mac(mac)}"))

    async def get_gateway_acls(self, site_id: str) -> list[dict]:
        return await self.get_paged(self._site(site_id, "/acls/osg-acls"))

    async def get_policy_routes(self, site_id: str) -> list[dict]:
        return await self.get_paged(self._site(site_id, "/routing/policy-routings"))

    async def get_lan_profiles(self, site_id: str) -> list[dict]:
        return await self.get_paged(self._site(site_id, "/lan-profiles"))

    async def set_switch_port_profile(
        self, site_id: str, mac: str, port: int, profile_id: str, *, port_name: str, profile_override: bool
    ) -> None:
        """Apply a LAN profile to a port, as the Omada UI does (the UI disables a port with
        the built-in "Disable" profile).

        Uses PATCH /switches/{mac}/ports/{port} with only name + profileId + the port's
        current profileOverrideEnable. The simpler PUT .../profile is rejected on 6.3 with
        -39855 for ports that keep their default name. Verified on SG2218P (6.3.0.45).
        """
        await self.request(
            "PATCH",
            self._site(site_id, f"/switches/{format_mac(mac)}/ports/{port}"),
            json={"name": port_name, "profileId": profile_id, "profileOverrideEnable": profile_override},
        )

    async def get_port_forwardings(self, site_id: str) -> list[dict]:
        return await self.get_paged(self._site(site_id, "/nat/port-forwardings"))

    async def get_wan_load_balance(self, site_id: str) -> dict:
        """Failover/load-balance config: linkBackup, primaryWans, backupWan, mode."""
        return await self.request("GET", self._site(site_id, "/internet/load-balance")) or {}

    async def get_dpi_enabled(self, site_id: str) -> bool:
        result = await self.request("GET", self._site(site_id, "/applicationControl/status")) or {}
        return bool(result.get("dpi"))

    async def get_client_wan_traffic(self, site_id: str, start: int, end: int) -> list[dict]:
        """Per-client internet traffic measured by the gateway DPI between two epoch seconds."""
        return await self.get_paged(self._site(site_id, "/dashboard/clientTraffic"), {"start": start, "end": end})

    async def get_wlan_groups(self, site_id: str) -> list[dict]:
        return await self.request("GET", self._site(site_id, "/wireless-network/wlans")) or []

    async def get_ssids(self, site_id: str, wlan_id: str) -> list[dict]:
        return await self.get_paged(self._site(site_id, f"/wireless-network/wlans/{wlan_id}/ssids"))

    async def find_client(self, mac: str) -> tuple[str, dict] | None:
        """Locate a client by MAC: (site_id, client) from active, then known clients."""
        mac = format_mac(mac)
        for site in await self.get_sites():
            sid = site["siteId"]
            for path in ("/clients", "/insight/clients"):
                for c in await self.get_paged(self._site(sid, path), {"searchKey": mac}):
                    if format_mac(c.get("mac", "")) == mac:
                        return sid, c
        return None

    async def reconnect_client(self, site_id: str, mac: str) -> None:
        await self.request("POST", self._site(site_id, f"/clients/{format_mac(mac)}/reconnect"))

    async def block_client(self, site_id: str, mac: str) -> None:
        await self.request("POST", self._site(site_id, f"/clients/{format_mac(mac)}/block"))

    async def unblock_client(self, site_id: str, mac: str) -> None:
        await self.request("POST", self._site(site_id, f"/clients/{format_mac(mac)}/unblock"))

    # ------------------------------------------------------------ write APIs
    # Implemented from the spec; NOT exercised yet (test phase is read-only).
    async def reboot_device(self, site_id: str, mac: str) -> None:
        await self.request("POST", self._site(site_id, f"/devices/{format_mac(mac)}/reboot"))

    async def set_switch_port_status(self, site_id: str, mac: str, port: int, on: bool) -> None:
        await self.request(
            "PUT",
            self._site(site_id, f"/switches/{format_mac(mac)}/ports/{port}/status"),
            json={"status": 1 if on else 0},
        )

    async def set_switch_port_poe(self, site_id: str, mac: str, port: int, on: bool) -> None:
        await self.request(
            "PUT",
            self._site(site_id, f"/switches/{format_mac(mac)}/ports/{port}/poe-mode"),
            json={"poeMode": 1 if on else 0},
        )

    async def set_ssid_enabled(self, site_id: str, ssid_id: str, on: bool) -> None:
        await self.request(
            "PATCH",
            self._site(site_id, f"/wireless-network/ssids/{ssid_id}/enable"),
            json={"ssidEnable": on},
        )

    async def set_gateway_acl_enabled(self, site_id: str, acl: dict, on: bool) -> None:
        """No status-only endpoint exists: PUT the full rule with ``status`` changed."""
        body = {k: acl[k] for k in ACL_PUT_FIELDS if k in acl}
        body["status"] = on
        await self.request("PUT", self._site(site_id, f"/acls/osg-acls/{acl['id']}"), json=body)

    async def set_policy_route_enabled(self, site_id: str, route: dict, on: bool) -> None:
        """No status-only endpoint exists: PUT the full route with ``status`` changed."""
        body = {k: route[k] for k in POLICY_ROUTE_PUT_FIELDS if k in route}
        body["status"] = on
        await self.request(
            "PUT", self._site(site_id, f"/routing/policy-routings/{route['id']}"), json=body
        )

    async def set_port_forwarding_enabled(self, site_id: str, rule: dict, on: bool) -> None:
        """No status-only endpoint exists: PUT the full rule with ``status`` changed."""
        body = {k: rule[k] for k in PORT_FORWARD_PUT_FIELDS if k in rule}
        body["status"] = on
        await self.request("PUT", self._site(site_id, f"/nat/port-forwardings/{rule['id']}"), json=body)


# Writable fields per the spec request schemas (modifyOsgAcl / modifyPolicyRouting /
# modifyPortForwarding).
ACL_PUT_FIELDS = (
    "description", "status", "policy", "protocols", "sourceIds", "destinationIds", "syslog",
    "sourceType", "destinationType", "direction", "stateMode", "states", "timeRangeId",
)
PORT_FORWARD_PUT_FIELDS = (
    "name", "status", "from", "limitedAddresses", "interfaceWanPortId", "virtualWanId", "existVirtualWan",
    "wanIps", "existWanIp", "externalPort", "forwardIp", "forwardPort", "protocol", "dMZ",
)
POLICY_ROUTE_PUT_FIELDS = (
    "name", "status", "protocols", "interfaceType", "interfaceId", "wanPortIds", "vpnIds",
    "virtualWanIds", "backupInterface", "sourceType", "sourceIds", "destinationType", "destinationIds",
)
