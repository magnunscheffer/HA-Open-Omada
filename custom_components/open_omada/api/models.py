"""Typed views over Omada Open API payloads.

Field meanings come from the controller's OpenAPI spec (see
docs/api-reference.md). Every model keeps the original payload in ``raw`` so
nothing is lost while the entity model evolves.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any

DEVICE_STATUS = {
    0: "disconnected",
    1: "connected",
    2: "pending",
    3: "heartbeat_missed",
    4: "isolated",
}

# Switch stat linkSpeed / WAN speed codes
LINK_SPEED_MBPS = {1: 10, 2: 100, 3: 1000, 4: 10000, 5: 10000, 6: 5000, 7: 2500}

DEVICE_TYPE_GATEWAY = "gateway"
DEVICE_TYPE_SWITCH = "switch"
DEVICE_TYPE_AP = "ap"


def _is_public_ip(value: str | None) -> bool:
    if not value:
        return False
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    # 100.64.0.0/10 (CGNAT) is not "global" either.
    return ip.is_global


_UPTIME_RE = re.compile(r"(?:(\d+)\s*day\(s\))?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?")


def parse_uptime(value: str | None) -> int | None:
    """'9day(s) 12h 15m 25s' -> seconds."""
    if not value or not (m := _UPTIME_RE.fullmatch(value.strip())) or not any(m.groups()):
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


@dataclass(slots=True)
class Site:
    id: str
    name: str
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> Site:
        return cls(id=d["siteId"], name=d.get("name", d["siteId"]), raw=d)


@dataclass(slots=True)
class Device:
    """Entry of GET /sites/{siteId}/devices (common to every device type)."""

    mac: str
    name: str
    type: str
    model: str
    status_code: int | None
    firmware: str | None
    ip: str | None
    uptime: str | None
    cpu: int | None
    mem: int | None
    serial: str | None
    raw: dict[str, Any] = field(repr=False)

    @property
    def status(self) -> str:
        return DEVICE_STATUS.get(self.status_code, f"unknown({self.status_code})")

    @property
    def uptime_seconds(self) -> int | None:
        return parse_uptime(self.uptime)

    @property
    def is_agile_switch(self) -> bool:
        return self.type == DEVICE_TYPE_SWITCH and self.raw.get("subtype") == "es"

    @property
    def online(self) -> bool:
        return self.status_code == 1

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> Device:
        return cls(
            mac=d["mac"],
            name=d.get("name") or d["mac"],
            type=d.get("type", "unknown"),
            model=d.get("model") or d.get("modelName") or "",
            status_code=d.get("status"),
            firmware=d.get("firmwareVersion"),
            ip=d.get("ip"),
            uptime=d.get("uptime"),
            cpu=d.get("cpuUtil"),
            mem=d.get("memUtil"),
            serial=d.get("sn"),
            raw=d,
        )


@dataclass(slots=True)
class Wan:
    """Entry of GET /gateways/{mac}/wan-status (only ports in WAN mode)."""

    port: int
    name: str
    connected: bool
    internet: bool
    ip: str | None
    proto: str | None
    rx_rate_kbps: float | None  # API gives KB/s; converted to kbit/s
    tx_rate_kbps: float | None
    rx_bytes: int | None
    tx_bytes: int | None
    latency_ms: int | None
    loss_pct: float | None
    raw: dict[str, Any] = field(repr=False)
    role: str | None = None  # "primary" | "backup" | "load_balance" (from load-balance config)
    in_use: bool | None = None  # carrying traffic right now (derived; None if unknown)

    @property
    def ip_is_public(self) -> bool:
        """True when the WAN interface itself holds a globally routable IP.

        The Open API has no per-WAN public IP; behind a modem/CGNAT this is
        the private address handed to the gateway.
        """
        return _is_public_ip(self.ip)

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> Wan:
        def kbps(v: Any) -> float | None:
            return None if v is None else round(float(v) * 8, 1)

        return cls(
            port=d["port"],
            name=d.get("name") or f"WAN{d['port']}",
            connected=d.get("status") == 1,
            internet=d.get("internetState") == 1,
            ip=d.get("ip") or None,
            proto=d.get("proto"),
            rx_rate_kbps=kbps(d.get("rxRate")),
            tx_rate_kbps=kbps(d.get("txRate")),
            rx_bytes=d.get("rx"),
            tx_bytes=d.get("tx"),
            latency_ms=d.get("latency"),
            loss_pct=d.get("loss"),
            raw=d,
        )


@dataclass(slots=True)
class GatewayLanPort:
    """A gateway port in LAN mode (from the gateway detail ``portStats``)."""

    port: int
    name: str
    link_up: bool
    speed_mbps: int | None
    poe_active: bool | None  # None when the gateway has no PoE

    @classmethod
    def from_api(cls, d: dict[str, Any], supports_poe: bool) -> GatewayLanPort:
        link = d.get("status") == 1
        return cls(
            port=d["port"],
            name=d.get("name") or f"LAN{d['port']}",
            link_up=link,
            speed_mbps=LINK_SPEED_MBPS.get(d.get("speed")) if link else None,
            poe_active=bool(d.get("poe")) if supports_poe else None,
        )


@dataclass(slots=True)
class Gateway:
    device: Device
    cpu: int | None
    mem: int | None
    temperature: int | None
    wans: list[Wan]
    raw: dict[str, Any] = field(repr=False)
    lan_ports: list[GatewayLanPort] = field(default_factory=list)


@dataclass(slots=True)
class SwitchPort:
    port: int
    name: str
    enabled: bool  # False when profile is the site's "Disable" profile or status is 0
    status_on: bool  # raw info.portList[].status (legacy port on/off flag)
    link_up: bool | None
    speed_mbps: int | None
    poe_enabled: bool | None  # admin PoE mode; None if the port has no PoE
    poe_active: bool | None  # currently powering a device
    poe_power_w: float | None  # current PoE draw (from /switches/ports/poe-info)
    lag_member: bool
    profile: str | None
    profile_id: str | None
    profile_override: bool
    native_network_id: str | None  # from /switches/ports/overview
    network_tags_setting: int | None  # 0 allow all, 1 block all, 2 custom (tagged VLAN lists)
    rx_bytes: int | None
    tx_bytes: int | None


@dataclass(slots=True)
class Switch:
    device: Device
    cpu: int | None
    mem: int | None
    supports_poe: bool
    ports: list[SwitchPort]
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_api(
        cls,
        device: Device,
        info: dict[str, Any],
        stat: dict[str, Any] | None,
        poe_info: dict[int, dict[str, Any]] | None = None,
        disable_profile_id: str | None = None,
        overview: dict[int, dict[str, Any]] | None = None,
    ) -> Switch:
        """Merge switch info (admin), stat (link) and site poe-info (per-port PoE).

        ``poe_info`` (port -> row of /switches/ports/poe-info) is authoritative for
        PoE capability: the info endpoint reports ``poeMode`` even on ports without
        PoE hardware. Without it, fall back to "every non-SFP port of a PoE switch".
        """
        stat = stat or {}
        supports_poe = bool(stat.get("supportPoe"))
        stat_ports = {p["port"]: p for p in stat.get("ports") or [] if isinstance(p, dict) and "port" in p}
        ports: list[SwitchPort] = []
        for p in info.get("portList") or []:
            if not isinstance(p, dict) or p.get("port") is None:
                continue
            s = stat_ports.get(p["port"], {})
            ps = s.get("portStatus") or {}
            link_up = None if not ps else ps.get("linkStatus") == 1
            # stat type: 1 copper, 2 combo, 3 SFP. SFP ports never do PoE.
            ov = (overview or {}).get(p["port"]) or {}
            row = (poe_info or {}).get(p["port"])
            if row is not None:
                poe_capable = bool(row.get("supportPoe"))
            else:
                poe_capable = supports_poe and "poeMode" in p and s.get("type") != 3
            power = row.get("power") if row else None
            ports.append(
                SwitchPort(
                    port=p["port"],
                    name=p.get("name") or f"Port{p['port']}",
                    enabled=p.get("status", 1) == 1
                    and (disable_profile_id is None or p.get("profileId") != disable_profile_id),
                    status_on=p.get("status", 1) == 1,
                    link_up=link_up,
                    speed_mbps=LINK_SPEED_MBPS.get(ps.get("linkSpeed")) if link_up else None,
                    poe_enabled=(p.get("poeMode") == 1) if poe_capable else None,
                    poe_active=(bool(ps.get("poe")) or bool(power)) if poe_capable else None,
                    poe_power_w=(float(power) if power is not None else 0.0) if poe_capable and row else None,
                    lag_member=bool(p.get("lagPort")),
                    profile=p.get("profileName"),
                    profile_id=p.get("profileId"),
                    profile_override=bool(p.get("profileOverrideEnable")),
                    native_network_id=ov.get("nativeNetworkId"),
                    network_tags_setting=ov.get("networkTagsSetting"),
                    rx_bytes=ps.get("rx"),
                    tx_bytes=ps.get("tx"),
                )
            )
        return cls(
            device=device,
            cpu=info.get("cpuUtil", device.cpu),
            mem=info.get("memUtil", device.mem),
            supports_poe=supports_poe,
            ports=ports,
            raw={"info": info, "stat": stat},
        )


@dataclass(slots=True)
class Ssid:
    id: str
    name: str
    enabled: bool
    wlan_id: str
    band: int | None
    broadcast: bool | None
    guest: bool | None
    vlan_id: int | None
    raw: dict[str, Any] = field(repr=False)

    @property
    def bands(self) -> list[str]:
        """Band bitmask: bit0 2.4G, bit1 5G, bit2 6G."""
        b = self.band or 0
        return [n for bit, n in ((1, "2.4G"), (2, "5G"), (4, "6G")) if b & bit]

    @classmethod
    def from_api(cls, d: dict[str, Any], wlan_id: str) -> Ssid:
        return cls(
            id=d.get("id") or d["ssidId"],
            name=d.get("name", ""),
            enabled=bool(d.get("ssidEnable", True)),
            wlan_id=wlan_id,
            band=d.get("band"),
            broadcast=d.get("broadcast"),
            guest=d.get("guestNetEnable"),
            vlan_id=d.get("vlanId") if d.get("vlanEnable") else None,
            raw=d,
        )


@dataclass(slots=True)
class AccessPoint:
    device: Device
    cpu: int | None
    mem: int | None
    wlan_id: str | None
    ssids: list[Ssid]
    raw: dict[str, Any] = field(repr=False)


@dataclass(slots=True)
class Rule:
    """Gateway ACL or policy route: anything with an on/off status."""

    id: str
    index: int | None
    name: str
    enabled: bool
    kind: str  # "acl" | "policy_route" | "port_forward" | "vpn"
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def acl(cls, d: dict[str, Any]) -> Rule:
        return cls(d["id"], d.get("index"), d.get("description") or d["id"],
                   bool(d.get("status")), "acl", d)

    @classmethod
    def policy_route(cls, d: dict[str, Any]) -> Rule:
        return cls(d["id"], d.get("index"), d.get("name") or d["id"],
                   bool(d.get("status")), "policy_route", d)


    @classmethod
    def port_forward(cls, d: dict[str, Any]) -> Rule:
        return cls(d["id"], None, d.get("name") or d["id"], bool(d.get("status")), "port_forward", d)

    @classmethod
    def vpn(cls, d: dict[str, Any]) -> Rule:
        return cls(d["id"], None, d.get("name") or d["id"], bool(d.get("status")), "vpn", d)


@dataclass(slots=True)
class WanClient:
    """Per-client internet usage from the gateway DPI (GET /dashboard/clientTraffic)."""

    mac: str
    name: str
    download_bytes: int
    upload_bytes: int

    @property
    def total_bytes(self) -> int:
        return self.download_bytes + self.upload_bytes

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> WanClient:
        return cls(
            mac=d.get("mac", ""),
            name=d.get("clientName") or d.get("mac", ""),
            download_bytes=int(d.get("download") or 0),
            upload_bytes=int(d.get("upload") or 0),
        )


@dataclass(slots=True)
class TopWanClients:
    dpi_enabled: bool
    window_s: int
    clients: list[WanClient]
    # End of the window actually used (epoch s). With look-back it can be older than "now"
    # because the gateway DPI reaches the controller in delayed batches.
    data_until: int | None = None
    delayed: bool = False


def pick_builtin_profiles(profiles: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """Return (disable_profile_id, default_profile_id) from GET /lan-profiles.

    Built-in profiles: type 1 = "Disable", type 0 = "All". Fall back to the names.
    """
    def find(ptype: int, name: str) -> str | None:
        for p in profiles:
            if p.get("type") == ptype:
                return p.get("id") or p.get("profileId")
        for p in profiles:
            if (p.get("name") or "").lower() == name:
                return p.get("id") or p.get("profileId")
        return None

    return find(1, "disable"), find(0, "all")


def apply_wan_roles(wans: list[Wan], lb: dict[str, Any] | None) -> str:
    """Set ``role``/``in_use`` on each WAN from GET /internet/load-balance.

    WAN ids look like ``"<port>_<uuid>"``. Returns the mode: "failover" or "load_balance".
    """
    lb = lb or {}
    def port_of(wan_id: Any) -> int | None:
        head = str(wan_id).split("_", 1)[0]
        return int(head) if head.isdigit() else None

    if not lb.get("linkBackup"):
        for w in wans:
            w.role, w.in_use = "load_balance", w.internet
        return "load_balance"
    primaries = {p for i in lb.get("primaryWans") or [] if (p := port_of(i)) is not None}
    backup = port_of(lb["backupWan"]) if lb.get("backupWan") else None
    prim = [w for w in wans if w.port in primaries]
    # mode 0: backup takes over when ANY primary fails; 1: when ALL fail; other: schedule.
    mode = lb.get("mode")
    if mode == 0:
        primary_failed = any(not w.internet for w in prim)
    elif mode == 1:
        primary_failed = all(not w.internet for w in prim)
    else:
        primary_failed = None
    for w in wans:
        if w.port in primaries:
            w.role, w.in_use = "primary", w.internet
        elif w.port == backup:
            w.role = "backup"
            w.in_use = None if primary_failed is None else (primary_failed and w.internet)
    return "failover"


@dataclass(slots=True)
class SiteSnapshot:
    site: Site
    devices: list[Device]
    gateways: list[Gateway]
    switches: list[Switch]
    access_points: list[AccessPoint]
    acls: list[Rule]
    policy_routes: list[Rule]
    ssids: list[Ssid]
    errors: list[str] = field(default_factory=list)
    port_forwards: list[Rule] = field(default_factory=list)
    vpns: list[Rule] = field(default_factory=list)
    # Switch port profiles: the built-in "Disable" (type 1) and default "All" (type 0).
    disable_profile_id: str | None = None
    default_profile_id: str | None = None
    wan_mode: str | None = None  # "failover" | "load_balance"


@dataclass(slots=True)
class ControllerSnapshot:
    info: dict[str, Any]
    sites: list[SiteSnapshot]

    @property
    def version(self) -> str | None:
        return self.info.get("controllerVer")

    @property
    def omadac_id(self) -> str | None:
        return self.info.get("omadacId")
