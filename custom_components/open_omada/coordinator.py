"""DataUpdateCoordinator: one coordinated poll of the whole controller."""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import OmadaAuthError, OmadaClient, OmadaError
from .api.models import AccessPoint, ControllerSnapshot, Gateway, Rule, SiteSnapshot, Ssid, Switch, TopWanClients
from .api.snapshot import fetch_snapshot, fetch_top_wan_clients
from .const import DOMAIN, LOGGER, SCAN_INTERVAL, TOP_WAN_KEEP, TOP_WAN_WINDOWS

type OmadaConfigEntry = ConfigEntry[OmadaCoordinator]


@dataclass(slots=True)
class DeviceRef:
    """Where an Omada device lives in the snapshot."""

    site: SiteSnapshot
    node: Gateway | Switch | AccessPoint


class OmadaCoordinator(DataUpdateCoordinator[ControllerSnapshot]):
    """Fetch a full controller snapshot and index it for entities."""

    config_entry: OmadaConfigEntry

    def __init__(self, hass: HomeAssistant, entry: OmadaConfigEntry, client: OmadaClient) -> None:
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self.client = client
        self.controller_device_id: str | None = None
        self.wan_control = None  # WanControl when Enhanced WAN Control is enabled
        self.data_started_at: float = 0.0
        # unique_ids the platforms currently provide (filled by each platform).
        self.provided_unique_ids: set[str] = set()
        # Each platform's "already added" unique_ids; cleared for a device when it is removed so
        # its entities are recreated if the device comes back.
        self._known_sets: list[set[str]] = []
        # Last seen built-in port profile ids per site: (disable, default). Used when a poll could
        # not read /lan-profiles, so a "Disable" profile is never remembered as a port's profile.
        self._builtin_profiles: dict[str, tuple[str | None, str | None]] = {}
        self.devices: dict[str, DeviceRef] = {}
        self.ssids: dict[tuple[str, str], tuple[SiteSnapshot, Ssid]] = {}
        self.rules: dict[tuple[str, str, str], tuple[SiteSnapshot, Rule]] = {}
        # DPI "top WAN clients" per (site, window key), see TOP_WAN_WINDOWS.
        self.top_wan: dict[tuple[str, str], TopWanClients] = {}
        # Last non-"Disable" profile of each switch port ("<mac>_<port>" -> profileId),
        # persisted so a port can be re-enabled with the profile it had.
        self.port_profiles: dict[str, str] = {}
        self._profile_store: Store[dict[str, str]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.port_profiles")
        # Ports where the integration enabled profile override only to turn PoE off.
        self.poe_override_ports: set[str] = set()
        self._poe_store: Store[list[str]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.poe_override")
        self._top_wan_at: dict[tuple[str, str], float] = {}

    @property
    def multi_site(self) -> bool:
        return len(self.data.sites) > 1

    async def _async_update_data(self) -> ControllerSnapshot:
        started = time.monotonic()
        try:
            snap = await fetch_snapshot(self.client)
        except OmadaAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except OmadaError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        devices: dict[str, DeviceRef] = {}
        ssids: dict[tuple[str, str], tuple[SiteSnapshot, Ssid]] = {}
        rules: dict[tuple[str, str, str], tuple[SiteSnapshot, Rule]] = {}
        for site in snap.sites:
            for node in (*site.gateways, *site.switches, *site.access_points):
                devices[node.device.mac] = DeviceRef(site, node)
            for ssid in site.ssids:
                ssids[(site.site.id, ssid.id)] = (site, ssid)
            for rule in (*site.acls, *site.policy_routes, *site.port_forwards, *site.vpns):
                rules[(site.site.id, rule.kind, rule.id)] = (site, rule)
            for err in site.errors:
                LOGGER.debug("Site %s partial error: %s", site.site.name, err)

        for site in snap.sites:
            if site.gateways:
                for key in TOP_WAN_WINDOWS:
                    await self._refresh_top_wan(site.site.id, key)

        self._apply_builtin_profiles(snap)
        self._remember_port_profiles(snap)
        self._remove_stale_devices(set(self.devices) - set(devices))
        self.devices, self.ssids, self.rules = devices, ssids, rules
        self.data_started_at = started  # when the controller was read for this data
        return snap

    def new_known_set(self) -> set[str]:
        known: set[str] = set()
        self._known_sets.append(known)
        return known

    def _forget_device_entities(self, mac: str) -> None:
        prefix = f"{mac}_"
        for known in (*self._known_sets, self.provided_unique_ids):
            known.difference_update({uid for uid in known if uid.startswith(prefix)})

    def save_port_profiles(self) -> None:
        self._profile_store.async_delay_save(lambda: self.port_profiles, 10)

    async def async_flush_stores(self) -> None:
        """Write pending store data now (on unload), so nothing is written after removal."""
        await self._profile_store.async_save(self.port_profiles)
        await self._poe_store.async_save(sorted(self.poe_override_ports))

    def _apply_builtin_profiles(self, snap: ControllerSnapshot) -> None:
        for site in snap.sites:
            sid = site.site.id
            if site.disable_profile_id:
                self._builtin_profiles[sid] = (site.disable_profile_id, site.default_profile_id)
                continue
            cached = self._builtin_profiles.get(sid)
            if not cached:
                continue
            site.disable_profile_id, site.default_profile_id = cached
            for sw in site.switches:
                for port in sw.ports:
                    if port.profile_id == site.disable_profile_id:
                        port.enabled = False

    async def async_load_port_profiles(self) -> None:
        self.port_profiles = await self._profile_store.async_load() or {}
        self.poe_override_ports = set(await self._poe_store.async_load() or [])

    def save_poe_override_ports(self) -> None:
        self._poe_store.async_delay_save(lambda: sorted(self.poe_override_ports), 1)

    def _remember_port_profiles(self, snap: ControllerSnapshot) -> None:
        changed = False
        disable_ids = {d for d, _ in self._builtin_profiles.values() if d}
        for site in snap.sites:
            if not site.disable_profile_id:
                continue  # profiles unknown this poll: do not risk remembering "Disable"
            for sw in site.switches:
                for port in sw.ports:
                    if port.profile_id and port.profile_id != site.disable_profile_id \
                            and port.profile_id not in disable_ids:
                        key = f"{sw.device.mac}_{port.port}"
                        if self.port_profiles.get(key) != port.profile_id:
                            self.port_profiles[key] = port.profile_id
                            changed = True
        if changed:
            self.save_port_profiles()

    async def _refresh_top_wan(self, site_id: str, key: str) -> None:
        window_s, every_s, lookback_s = TOP_WAN_WINDOWS[key]
        slot = (site_id, key)
        if time.monotonic() - self._top_wan_at.get(slot, -every_s) < every_s:
            return
        prev = self.top_wan.get(slot)
        try:
            # Look back only on a cold start; afterwards keep the previous result instead.
            result = await fetch_top_wan_clients(
                self.client, site_id, window_s=window_s, lookback_s=0 if prev else lookback_s
            )
            self.top_wan[slot] = self._keep_previous_if_empty(prev, result)
        except OmadaAuthError as err:
            raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="auth_failed") from err
        except OmadaError as err:
            LOGGER.debug("Top WAN clients (%s) unavailable for site %s: %s", key, site_id, err)
            self.top_wan.pop(slot, None)
        self._top_wan_at[slot] = time.monotonic()

    @staticmethod
    def _keep_previous_if_empty(prev: TopWanClients | None, result: TopWanClients) -> TopWanClients:
        """DPI batches reach the controller late: an empty window usually means "not counted
        yet", not "no traffic". Keep the last result with data (marked delayed) for up to
        TOP_WAN_KEEP seconds; its data_until shows how old it is."""
        if not result.dpi_enabled or result.clients or not prev or not prev.clients or not prev.data_until:
            return result
        if time.time() - prev.data_until > TOP_WAN_KEEP:
            return result
        return dataclasses.replace(prev, delayed=True)

    def _remove_stale_devices(self, gone: set[str]) -> None:
        """Remove devices that disappeared from the controller (forgotten/moved)."""
        if not gone:
            return
        registry = dr.async_get(self.hass)
        for mac in gone:
            device = registry.async_get_device_by_identifier((DOMAIN, mac), self.config_entry.entry_id)
            if device:
                LOGGER.info("Removing Omada device %s no longer on the controller", mac)
                registry.async_remove_device(device.id)
            self._forget_device_entities(mac)
