"""Builds a full, hierarchical snapshot of the controller.

This is what the Home Assistant DataUpdateCoordinator will call on every
refresh. Calls are sequential per site (the client semaphore also limits
concurrency) to stay gentle with the controller. A failure on one device or
feature is recorded in ``SiteSnapshot.errors`` instead of aborting the
whole refresh.
"""

from __future__ import annotations

import logging
import time

from .client import OmadaClient
from .exceptions import OmadaAuthError, OmadaConnectionError, OmadaError
from .models import (
    DEVICE_TYPE_AP,
    DEVICE_TYPE_GATEWAY,
    DEVICE_TYPE_SWITCH,
    AccessPoint,
    ControllerSnapshot,
    Device,
    Gateway,
    Rule,
    Site,
    SiteSnapshot,
    Ssid,
    Switch,
    TopWanClients,
    Wan,
    WanClient,
    apply_wan_roles,
    pick_builtin_profiles,
)

_LOGGER = logging.getLogger(__name__)


async def _safe(errors: list[str], label: str, coro):
    try:
        return await coro
    except (OmadaAuthError, OmadaConnectionError):
        raise  # global problems: let the caller decide
    except OmadaError as err:
        _LOGGER.debug("%s failed: %s", label, err)
        errors.append(f"{label}: {err}")
        return None


def _parse(errors: list[str], label: str, items, fn) -> list:
    """Convert API items one by one; a malformed item is skipped and recorded, not fatal."""
    out = []
    for item in items or []:
        try:
            out.append(fn(item))
        except (KeyError, TypeError, ValueError, AttributeError) as err:
            errors.append(f"{label}: skipped malformed item ({type(err).__name__}: {err})")
    return out


async def fetch_site(client: OmadaClient, site: Site) -> SiteSnapshot:
    sid = site.id
    errors: list[str] = []

    devices = _parse(errors, "devices", await client.get_devices(sid), Device.from_api)

    # --- SSIDs (site level, grouped by WLAN group) -------------------------
    ssids: list[Ssid] = []
    for wlan in await _safe(errors, "wlan groups", client.get_wlan_groups(sid)) or []:
        wid = wlan.get("wlanId") or wlan.get("id")
        raw_ssids = await _safe(errors, f"ssids {wid}", client.get_ssids(sid, wid))
        ssids.extend(_parse(errors, f"ssids {wid}", raw_ssids, lambda s, wid=wid: Ssid.from_api(s, wid)))

    poe_rows: dict[str, dict[int, dict]] = {}
    overview_rows: dict[str, dict[int, dict]] = {}
    disable_pid = default_pid = None
    if any(d.type == DEVICE_TYPE_SWITCH for d in devices):
        profiles = await _safe(errors, "lan profiles", client.get_lan_profiles(sid)) or []
        disable_pid, default_pid = pick_builtin_profiles(profiles)
    if any(d.type == DEVICE_TYPE_SWITCH and d.online for d in devices):
        for row in await _safe(errors, "switch poe info", client.get_switch_ports_poe(sid)) or []:
            if isinstance(row, dict) and row.get("switchMac") and row.get("port") is not None:
                poe_rows.setdefault(row["switchMac"], {})[row["port"]] = row
        for row in await _safe(errors, "switch ports overview", client.get_switch_ports_overview(sid)) or []:
            if isinstance(row, dict) and row.get("switchMac") and row.get("port") is not None:
                overview_rows.setdefault(row["switchMac"], {})[row["port"]] = row

    gateways: list[Gateway] = []
    switches: list[Switch] = []
    aps: list[AccessPoint] = []

    for dev in devices:
        if not dev.online:
            # Offline devices are still listed, without live details.
            if dev.type == DEVICE_TYPE_GATEWAY:
                gateways.append(Gateway(dev, dev.cpu, dev.mem, None, [], {}))
            elif dev.type == DEVICE_TYPE_SWITCH:
                switches.append(Switch(dev, dev.cpu, dev.mem, False, [], {}))
            elif dev.type == DEVICE_TYPE_AP:
                aps.append(AccessPoint(dev, dev.cpu, dev.mem, None, [], {}))
            continue

        if dev.type == DEVICE_TYPE_GATEWAY:
            info = await _safe(errors, f"gateway {dev.name}", client.get_gateway(sid, dev.mac)) or {}
            wan_raw = (
                await _safe(errors, f"wan {dev.name}", client.get_gateway_wan_status(sid, dev.mac)) or []
            )
            wan_ports = [w for w in wan_raw if isinstance(w, dict) and w.get("mode", 0) == 0]
            wans = _parse(errors, f"wan {dev.name}", wan_ports, Wan.from_api)
            gateways.append(
                Gateway(dev, info.get("cpuUtil", dev.cpu), info.get("memUtil", dev.mem),
                        info.get("temp"), wans, {"info": info, "wan": wan_raw})
            )
        elif dev.type == DEVICE_TYPE_SWITCH:
            call = client.get_switch(sid, dev.mac, agile=dev.is_agile_switch)
            info = await _safe(errors, f"switch {dev.name}", call) or {}
            stat = await _safe(errors, f"switch stat {dev.name}", client.get_switch_stat(sid, dev.mac))
            switches.append(Switch.from_api(
                dev, info, stat, poe_rows.get(dev.mac), disable_pid, overview_rows.get(dev.mac)
            ))
        elif dev.type == DEVICE_TYPE_AP:
            info = await _safe(errors, f"ap {dev.name}", client.get_ap(sid, dev.mac)) or {}
            wid = info.get("wlanId")
            aps.append(
                AccessPoint(dev, info.get("cpuUtil", dev.cpu), info.get("memUtil", dev.mem), wid,
                            [s for s in ssids if s.wlan_id == wid], info)
            )

    has_gateway = any(d.type == DEVICE_TYPE_GATEWAY for d in devices)
    acls: list[Rule] = []
    routes: list[Rule] = []
    forwards: list[Rule] = []
    wan_mode: str | None = None
    if has_gateway:
        acls = _parse(errors, "acls", await _safe(errors, "acls", client.get_gateway_acls(sid)), Rule.acl)
        routes = _parse(errors, "policy routes",
                        await _safe(errors, "policy routes", client.get_policy_routes(sid)), Rule.policy_route)
        forwards = _parse(errors, "port forwarding",
                          await _safe(errors, "port forwarding", client.get_port_forwardings(sid)), Rule.port_forward)
        lb = await _safe(errors, "wan load balance", client.get_wan_load_balance(sid))
        if lb is not None:
            for gw in gateways:
                wan_mode = apply_wan_roles(gw.wans, lb)

    return SiteSnapshot(site, devices, gateways, switches, aps, acls, routes, ssids, errors,
                        port_forwards=forwards, wan_mode=wan_mode,
                        disable_profile_id=disable_pid, default_profile_id=default_pid)


async def fetch_snapshot(client: OmadaClient, site_ids: list[str] | None = None) -> ControllerSnapshot:
    info = client.controller_info or await client.get_controller_info()
    sites = [Site.from_api(s) for s in await client.get_sites()]
    if site_ids:
        sites = [s for s in sites if s.id in site_ids]
    return ControllerSnapshot(info, [await fetch_site(client, s) for s in sites])


async def fetch_top_wan_clients(
    client: OmadaClient, site_id: str, *, window_s: int = 3600, limit: int = 5, now: int | None = None
) -> TopWanClients:
    """Top internet consumers over the last ``window_s`` seconds (needs gateway DPI)."""
    if not await client.get_dpi_enabled(site_id):
        return TopWanClients(False, window_s, [])
    end = int(now if now is not None else time.time())
    rows = await client.get_client_wan_traffic(site_id, end - window_s, end)
    ranked = sorted((WanClient.from_api(r) for r in rows), key=lambda c: c.total_bytes, reverse=True)
    return TopWanClients(True, window_s, [c for c in ranked if c.total_bytes > 0][:limit])
