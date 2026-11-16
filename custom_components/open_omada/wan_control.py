"""Enhanced WAN Control: per-WAN quality probes and automatic failover.

* Probe targets are pinned to a WAN with Omada objects managed here (named
  ``Open Omada probe <tag> …``): an IP group with this Home Assistant host, one IP group with
  the probe IPs of each WAN and one policy route per WAN (HA → probes → that WAN, no fallback),
  kept at the top of the policy route list.
* Every ``interval`` seconds both WANs are measured (TLS handshake probes, confirmed like the
  owner's original script).
* Mode ``automatic`` swaps primary/backup (PUT /internet/load-balance) when the WAN in use is
  Poor/Offline and the other one is Good; it returns to the preferred WAN only after the
  preferred WAN has been Good for ``failback`` minutes in a row (any non-Good result restarts
  the count). ``notify`` only fires events; ``off`` only measures.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import instance_id
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .api import OmadaError
from .api.wanprobe import GOOD, OFFLINE, POOR, ProbeTarget, WanQuality, measure_wan, resolve_ipv4
from .const import DOMAIN, LOGGER, MANAGED_PREFIX

SIGNAL_WAN_CONTROL = f"{DOMAIN}_wan_control_update"
EVENT_WAN_FAILOVER = f"{DOMAIN}_wan_failover"
MODES = ("off", "notify", "automatic")
RESYNC_EVERY = 3600  # re-resolve probe hosts / re-check managed objects


@dataclass(slots=True)
class WanControlConfig:
    probes: dict[int, list[str]]
    latency_ms: float = 150
    interval_s: int = 30
    confirm_tests: int = 2
    confirm_interval_s: float = 5
    cooldown_min: float = 15
    failback_min: float = 60


@dataclass(slots=True)
class WanState:
    port: int
    name: str
    targets: list[ProbeTarget] = field(default_factory=list)
    quality: WanQuality | None = None
    good_since: float | None = None


class WanControl:
    def __init__(self, hass: HomeAssistant, coordinator, config: WanControlConfig) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.client = coordinator.client
        self.config = config
        self.mode = "notify"
        self.preferred_port: int | None = None
        self.wans: dict[int, WanState] = {}
        self.tag = ""
        self.site_id: str | None = None
        self.last_switch_at: float | None = None
        self.last_action: dict[str, Any] | None = None
        self.error: str | None = None
        self.skipped: dict[int, list[str]] = {}  # probes that cannot be pinned (WAN DNS servers)
        self._notified: str | None = None
        self._synced_at = 0.0
        self._unsub = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- lifecycle
    async def async_start(self) -> None:
        self.tag = (await instance_id.async_get(self.hass))[:6]
        await self._sync_objects()
        self._unsub = async_track_time_interval(
            self.hass, self._async_cycle, timedelta(seconds=self.config.interval_s)
        )
        self.hass.async_create_task(self._async_cycle())

    @callback
    def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    # --------------------------------------------------------- site helpers
    def _site(self):
        for site in self.coordinator.data.sites:
            if site.gateways and len(site.gateways[0].wans) >= 2:
                return site
        return None

    @staticmethod
    def wan_ids(site) -> dict[int, str]:
        lb = site.load_balance or {}
        ids = [*(lb.get("primaryWans") or []), lb.get("backupWan")]
        out: dict[int, str] = {}
        for wan_id in ids:
            head = str(wan_id or "").split("_", 1)[0]
            if head.isdigit():
                out[int(head)] = wan_id
        return out

    def primary_port(self) -> int | None:
        site = self._site()
        if not site:
            return None
        prim = (site.load_balance or {}).get("primaryWans") or []
        head = str(prim[0]).split("_", 1)[0] if prim else ""
        return int(head) if head.isdigit() else None

    def in_use_port(self) -> int | None:
        site = self._site()
        if not site:
            return None
        used = [w.port for w in site.gateways[0].wans if w.in_use]
        return used[0] if len(used) == 1 else self.primary_port()

    # ------------------------------------------------------- managed objects
    def _name(self, what: str) -> str:
        return f"{MANAGED_PREFIX}{self.tag} {what}"

    async def _sync_objects(self) -> None:
        """Create/update the IP groups and policy routes that pin probes to each WAN."""
        from homeassistant.components.network import async_get_source_ip

        site = self._site()
        if site is None:
            self.error = "No gateway with two WANs in failover mode"
            return
        sid = self.site_id = site.site.id
        wan_ids = self.wan_ids(site)
        ha_ip = await async_get_source_ip(self.hass, "1.1.1.1")
        groups = {g.get("name"): g.get("groupId") or g.get("id") for g in await self.client.get_ip_groups(sid)}

        async def ensure_group(name: str, ips: list[str]) -> str:
            if name in groups:
                await self.client.update_ip_group(sid, groups[name], name, ips)
                return groups[name]
            gid = await self.client.create_ip_group(sid, name, ips)
            if not gid:  # some versions do not return the id
                gid = {g.get("name"): g.get("groupId") or g.get("id")
                       for g in await self.client.get_ip_groups(sid)}.get(name)
            return gid

        source = await ensure_group(self._name("HA"), [ha_ip])
        routes = {r.get("name"): r for r in await self.client.get_policy_routes(sid)}
        try:
            wan_dns = await self.client.get_wan_dns_servers(sid)
        except OmadaError as err:
            LOGGER.debug("Could not read the WAN DNS servers: %s", err)
            wan_dns = set()
        self.skipped = {}
        for wan in site.gateways[0].wans:
            if wan.port not in wan_ids:
                continue
            hosts = self.config.probes.get(wan.port, [])
            targets: list[ProbeTarget] = []
            for host in hosts:
                try:
                    ips = await resolve_ipv4(host)
                except OSError as err:
                    LOGGER.warning("Probe host %s does not resolve: %s", host, err)
                    continue
                if ips and ips[0] in wan_dns:
                    # The gateway routes its own WAN DNS servers itself: a policy route cannot pin them.
                    LOGGER.warning(
                        "Probe %s (%s) is a DNS server of a WAN on the gateway and cannot be pinned to "
                        "WAN%s; it is skipped. Choose another probe or change the WAN DNS", host, ips[0], wan.port,
                    )
                    self.skipped.setdefault(wan.port, []).append(host)
                elif ips:
                    targets.append(ProbeTarget(host, ips[0]))
            state = self.wans.setdefault(wan.port, WanState(wan.port, wan.name))
            state.name, state.targets = wan.name, targets
            if not targets:
                continue
            dest = await ensure_group(self._name(f"WAN{wan.port}"), sorted({t.ip for t in targets}))
            rname = self._name(f"WAN{wan.port}")
            if rname in routes:
                await self.client.update_probe_route(sid, routes[rname]["id"], rname, wan_ids[wan.port], source, dest)
            else:
                await self.client.create_probe_route(sid, rname, wan_ids[wan.port], source, dest)
        routes = sorted(await self.client.get_policy_routes(sid), key=lambda r: r.get("index") or 0)
        ours = [r["id"] for r in routes if str(r.get("name", "")).startswith(self._name(""))]
        others = [r["id"] for r in routes if r["id"] not in ours]
        if [r["id"] for r in routes][: len(ours)] != ours:
            await self.client.set_policy_route_order(sid, ours + others)
        if self.preferred_port is None:
            self.preferred_port = self.primary_port()
        self._synced_at = time.monotonic()
        self.error = None
        LOGGER.debug("WAN probes synced: %s", {p: [t.ip for t in w.targets] for p, w in self.wans.items()})

    async def async_remove_objects(self) -> None:
        """Delete every managed object of this HA instance (feature turned off)."""
        self.async_stop()
        await async_remove_managed_objects(self.hass, self.client)

    # ------------------------------------------------------------- the loop
    async def _async_cycle(self, _now=None) -> None:
        if self._lock.locked():
            return
        async with self._lock:
            try:
                if time.monotonic() - self._synced_at > RESYNC_EVERY:
                    await self._sync_objects()
                await self._measure()
                await self._decide()
            except OmadaError as err:
                self.error = str(err)
                LOGGER.warning("Enhanced WAN Control: %s", err)
            async_dispatcher_send(self.hass, SIGNAL_WAN_CONTROL)

    async def _measure(self) -> None:
        cfg = self.config
        wans = [w for w in self.wans.values() if w.targets]
        results = await asyncio.gather(*(
            measure_wan(w.targets, latency_ms=cfg.latency_ms, confirm_tests=cfg.confirm_tests,
                        confirm_interval=cfg.confirm_interval_s)
            for w in wans
        ))
        now = time.time()
        for wan, quality in zip(wans, results, strict=True):
            wan.quality = quality
            if quality.quality == GOOD:
                wan.good_since = wan.good_since or now
            else:
                wan.good_since = None  # any non-Good result restarts the failback count

    def stable_minutes(self, port: int | None) -> float:
        wan = self.wans.get(port) if port is not None else None
        return round((time.time() - wan.good_since) / 60, 1) if wan and wan.good_since else 0.0

    def cooldown_left_min(self) -> float:
        if self.last_switch_at is None:
            return 0.0
        return max(0.0, round(self.config.cooldown_min - (time.time() - self.last_switch_at) / 60, 1))

    async def _decide(self) -> None:
        if self.mode == "off" or len(self.wans) < 2:
            self._notified = None
            return
        active = self.in_use_port()
        if active is None or active not in self.wans:
            return
        other = next(p for p in self.wans if p != active)
        q_active = (self.wans[active].quality or None)
        q_other = (self.wans[other].quality or None)
        if not q_active or not q_other:
            return
        target, reason = None, None
        if q_active.quality in (POOR, OFFLINE) and q_other.quality == GOOD:
            target, reason = other, f"{self.wans[active].name} is {q_active.quality}"
        elif (self.preferred_port not in (None, active) and self.preferred_port in self.wans
              and self.stable_minutes(self.preferred_port) >= self.config.failback_min):
            target, reason = self.preferred_port, f"{self.wans[self.preferred_port].name} stable for " \
                                                  f"{self.config.failback_min:g} min"
        if target is None:
            self._notified = None
            return
        if self.cooldown_left_min() > 0:
            return
        key = f"{active}->{target}"
        if self.mode == "notify":
            if self._notified != key:
                self._notified = key
                self._fire(active, target, reason, dry_run=True)
            return
        site = self._site()
        await self.client.set_primary_wan(site.site.id, self.wan_ids(site)[target])
        self.last_switch_at = time.time()
        self._notified = None
        self._fire(active, target, reason, dry_run=False)
        await self.coordinator.async_request_refresh()

    def _fire(self, from_port: int, to_port: int, reason: str, *, dry_run: bool) -> None:
        data = {"from": self.wans[from_port].name, "to": self.wans[to_port].name, "reason": reason,
                "dry_run": dry_run}
        self.last_action = {**data, "at": time.time()}
        LOGGER.info("WAN failover %s: %s -> %s (%s)", "suggested" if dry_run else "done",
                    data["from"], data["to"], reason)
        self.hass.bus.async_fire(EVENT_WAN_FAILOVER, data)
        if dry_run:
            # Notify mode: a notification that stays until the user dismisses it. A fixed id
            # replaces the previous suggestion instead of piling them up.
            persistent_notification.async_create(
                self.hass,
                f"Suggested switch from **{data['from']}** to **{data['to']}**: {reason}.\n\n"
                "Auto WAN failover is set to *Notify only*, so nothing was changed. Use the "
                "*Primary WAN* select to switch now, or set *Auto WAN failover* to *Automatic*.",
                title="Open Omada: WAN failover suggested",
                notification_id=f"{DOMAIN}_wan_failover_{self.coordinator.config_entry.entry_id}",
            )

    # ------------------------------------------------------- manual control
    async def async_set_primary(self, port: int) -> None:
        site = self._site()
        if site is None or port not in self.wan_ids(site):
            raise OmadaError(f"WAN {port} is not part of the failover configuration")
        await self.client.set_primary_wan(site.site.id, self.wan_ids(site)[port])
        self.last_switch_at = time.time()
        await self.coordinator.async_request_refresh()

    def status(self) -> str:
        if self.error:
            return "error"
        if self.mode == "off":
            return "monitoring"
        active = self.in_use_port()
        if self.cooldown_left_min() > 0:
            return "cooldown"
        if self.preferred_port is not None and active is not None and active != self.preferred_port:
            return "failed_over"
        return "ok"


async def async_remove_managed_objects(hass: HomeAssistant, client) -> None:
    """Delete this HA instance's probe objects on every site (used when the entry is removed)."""
    prefix = f"{MANAGED_PREFIX}{(await instance_id.async_get(hass))[:6]} "
    await client.get_controller_info()
    for site in await client.get_sites():
        sid = site["siteId"]
        for r in await client.get_policy_routes(sid):
            if str(r.get("name", "")).startswith(prefix):
                await client.delete_policy_route(sid, r["id"])
        for g in await client.get_ip_groups(sid):
            if str(g.get("name", "")).startswith(prefix):
                await client.delete_ip_group(sid, g.get("groupId") or g.get("id"))
