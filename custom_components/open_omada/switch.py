"""Switches: SSIDs, gateway ACLs, policy routes, switch ports and PoE."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.models import Switch, SwitchPort
from .const import DOMAIN
from .coordinator import OmadaConfigEntry, OmadaCoordinator
from .entity import NewEntities, OmadaControllerEntity, OmadaDeviceEntity, OmadaGatewayChild

# Actions hit the controller; one at a time.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OmadaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    known = coordinator.new_known_set()

    @callback
    def _add_new() -> None:
        new = NewEntities(known)
        oid = coordinator.data.omadac_id
        for site_id, ssid_id in coordinator.ssids:
            new.add(f"{oid}_{site_id}_ssid_{ssid_id}",
                    lambda s=site_id, i=ssid_id: OmadaSsidSwitch(coordinator, s, i))
        for (site_id, kind, rule_id), (site, _rule) in coordinator.rules.items():
            if not site.gateways:
                continue
            gw = site.gateways[0].device.mac
            new.add(f"{gw}_{kind}_{rule_id}",
                    lambda s=site_id, k=kind, r=rule_id: OmadaRuleSwitch(coordinator, s, k, r))
        for mac, ref in coordinator.devices.items():
            if isinstance(ref.node, Switch):
                for port in ref.node.ports:
                    if not port.lag_member:
                        new.add(f"{mac}_port{port.port}_enabled",
                                lambda n=port.port, mac=mac: OmadaPortSwitch(coordinator, mac, n))
                    if port.poe_enabled is not None:
                        new.add(f"{mac}_port{port.port}_poe",
                                lambda n=port.port, mac=mac: OmadaPoeSwitch(coordinator, mac, n))
        coordinator.provided_unique_ids |= known
        if new.items:
            async_add_entities(new.items)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class _OptimisticSwitch(SwitchEntity):
    """is_on honours the optimistic state set after a successful action."""

    _optimistic: bool | None
    _actual_on: bool

    @property
    def is_on(self) -> bool:
        return self._optimistic if self._optimistic is not None else self._actual_on


class OmadaSsidSwitch(OmadaControllerEntity, _OptimisticSwitch):
    """SSID on/off. Site-wide, so it lives on the controller device."""

    _attr_translation_key = "ssid"

    def __init__(self, coordinator: OmadaCoordinator, site_id: str, ssid_id: str) -> None:
        super().__init__(coordinator, f"{site_id}_ssid_{ssid_id}")
        self._key = (site_id, ssid_id)
        site, ssid = coordinator.ssids[self._key]
        prefix = f"{site.site.name} " if coordinator.multi_site else ""
        self._attr_translation_placeholders = {"name": f"{prefix}{ssid.name}"}

    @property
    def available(self) -> bool:
        return super().available and self._key in self.coordinator.ssids

    @property
    def _actual_on(self) -> bool:
        return self.coordinator.ssids[self._key][1].enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        site, ssid = self.coordinator.ssids[self._key]
        return {
            "access_points": sorted(ap.device.name for ap in site.access_points if ap.wlan_id == ssid.wlan_id),
            "bands": ssid.bands,
            "vlan_id": ssid.vlan_id,
            "guest": ssid.guest,
            "broadcast": ssid.broadcast,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._call(self.coordinator.client.set_ssid_enabled(self._key[0], self._key[1], True), True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._call(self.coordinator.client.set_ssid_enabled(self._key[0], self._key[1], False), False)


class OmadaRuleSwitch(OmadaGatewayChild, _OptimisticSwitch):
    """Gateway ACL or policy route on/off, attached to the site's gateway."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: OmadaCoordinator, site_id: str, kind: str, rule_id: str) -> None:
        self._key = (site_id, kind, rule_id)
        rule = coordinator.rules[self._key][1]
        super().__init__(coordinator, site_id, f"{kind}_{rule_id}")
        self._attr_translation_key = kind
        self._attr_translation_placeholders = {"name": rule.name}

    @property
    def available(self) -> bool:
        return super().available and self._key in self.coordinator.rules

    @property
    def _actual_on(self) -> bool:
        return self.coordinator.rules[self._key][1].enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rule = self.coordinator.rules[self._key][1]
        if rule.kind == "port_forward":
            r = rule.raw
            return {"external_port": r.get("externalPort"), "forward_ip": r.get("forwardIp"),
                    "forward_port": r.get("forwardPort"), "protocol": r.get("protocol")}
        return {"index": rule.index}

    async def _set(self, on: bool) -> None:
        site_id, kind, rule_id = self._key
        client = self.coordinator.client

        async def write() -> None:
            # Re-read the rule: a PUT of a 60 s old copy would undo recent edits made in Omada.
            raw = await client.get_rule_live(site_id, kind, rule_id)
            if raw is None:
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="item_gone")
            setter = {"acl": client.set_gateway_acl_enabled, "port_forward": client.set_port_forwarding_enabled,
                      "policy_route": client.set_policy_route_enabled}[kind]
            await setter(site_id, raw, on)

        await self._call(write(), on)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class _PortEntity(OmadaDeviceEntity):
    def __init__(self, coordinator: OmadaCoordinator, mac: str, port: int, key: str) -> None:
        super().__init__(coordinator, mac, f"port{port}_{key}")
        self.port = port
        self._attr_translation_placeholders = {"port": str(port), "name": self._port.name}

    @property
    def _port(self) -> SwitchPort:
        node = self.ref.node
        assert isinstance(node, Switch)
        return next(p for p in node.ports if p.port == self.port)

    @property
    def available(self) -> bool:
        if not (super().available and self.device_online):
            return False
        node = self.ref.node
        return isinstance(node, Switch) and any(p.port == self.port for p in node.ports)


class OmadaPortSwitch(_PortEntity, _OptimisticSwitch):
    """Enable/disable a switch port the way the Omada UI does: via the "Disable" profile.

    Off: remember the current profile, apply "Disable". On: re-apply the remembered
    profile (or the site's default "All" if unknown) and clear a legacy status=0.
    """

    _attr_translation_key = "port"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: OmadaCoordinator, mac: str, port: int) -> None:
        super().__init__(coordinator, mac, port, "enabled")

    @property
    def _actual_on(self) -> bool:
        return self._port.enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"profile": self._port.profile}

    @property
    def _key(self) -> str:
        return f"{self.mac}_{self.port}"

    async def _live(self) -> dict:
        """Port config read now: the snapshot can be up to a minute (or one action) old."""
        live = await self.coordinator.client.get_switch_port_live(self.site_id, self.mac, self.port)
        if live is None or not live.get("profileId"):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="item_gone")
        return live

    async def _apply(self, on: bool) -> None:
        site = self.ref.site
        client = self.coordinator.client
        port = self._port
        live = await self._live()
        name = live.get("portName") or port.name
        override = bool(live.get("profileOverrideEnable"))
        current = live["profileId"]
        if on:
            target = self.coordinator.port_profiles.get(self._key) or site.default_profile_id
            if target is None:
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_profile")
            if current != target:
                await client.set_switch_port_profile(
                    self.site_id, self.mac, self.port, target, port_name=name, profile_override=override
                )
            if not port.status_on:
                await client.set_switch_port_status(self.site_id, self.mac, self.port, True)
        else:
            if site.disable_profile_id is None:
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_disable_profile")
            if current != site.disable_profile_id:
                self.coordinator.port_profiles[self._key] = current
                self.coordinator.save_port_profiles()
            await client.set_switch_port_profile(
                self.site_id, self.mac, self.port, site.disable_profile_id, port_name=name, profile_override=override
            )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._call(self._apply(True), True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._call(self._apply(False), False)


class OmadaPoeSwitch(_PortEntity, _OptimisticSwitch):
    """PoE on/off of a switch port.

    On Omada >= 6.2.10 PoE only changes with profile override on. For a port that
    follows its profile: off = enable override + PoE off (remembered); on = PoE on, then
    override off again so the port follows its profile. Ports where the user enabled
    override themselves only get PoE toggled.
    """

    _attr_translation_key = "poe"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: OmadaCoordinator, mac: str, port: int) -> None:
        super().__init__(coordinator, mac, port, "poe")

    @property
    def _actual_on(self) -> bool:
        return bool(self._port.poe_enabled)

    @property
    def _key(self) -> str:
        return f"{self.mac}_{self.port}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"override_by_integration": self._key in self.coordinator.poe_override_ports}

    async def _apply(self, on: bool) -> None:
        port, client, coord = self._port, self.coordinator.client, self.coordinator
        live = await client.get_switch_port_live(self.site_id, self.mac, self.port)
        if live is None or not live.get("profileId"):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="item_gone")
        name = live.get("portName") or port.name
        profile_id = live["profileId"]
        override = bool(live.get("profileOverrideEnable"))
        native, tags = live.get("nativeNetworkId"), live.get("networkTagsSetting")
        ours = self._key in coord.poe_override_ports
        if override and not ours:
            await client.set_switch_port_poe(self.site_id, self.mac, self.port, on)
            return
        if tags not in (0, 1) or not native:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="poe_needs_override")
        await client.set_switch_port_poe_via_override(
            self.site_id, self.mac, self.port, on,
            port_name=name, profile_id=profile_id, native_network_id=native, network_tags_setting=tags,
        )
        if on:
            await client.set_switch_port_follow_profile(
                self.site_id, self.mac, self.port, port_name=name, profile_id=profile_id
            )
            coord.poe_override_ports.discard(self._key)
        else:
            coord.poe_override_ports.add(self._key)
        coord.save_poe_override_ports()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._call(self._apply(True), True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._call(self._apply(False), False)
