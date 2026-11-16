"""Binary sensors: device online, WAN link/internet, switch port link, PoE powering."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.models import Gateway, Switch
from .coordinator import OmadaConfigEntry, OmadaCoordinator
from .entity import OmadaDeviceEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OmadaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new: list[BinarySensorEntity] = []
        for mac, ref in coordinator.devices.items():
            cands: list[BinarySensorEntity] = [OmadaOnlineSensor(coordinator, mac)]
            if isinstance(ref.node, Gateway):
                for w in ref.node.wans:
                    cands += [OmadaWanBinary(coordinator, mac, w.port, "link"),
                              OmadaWanBinary(coordinator, mac, w.port, "internet"),
                              OmadaWanBinary(coordinator, mac, w.port, "in_use")]
            if isinstance(ref.node, Switch):
                for p in ref.node.ports:
                    cands.append(OmadaPortLink(coordinator, mac, p.port))
                    if p.poe_active is not None:
                        cands.append(OmadaPoePowering(coordinator, mac, p.port))
            for e in cands:
                if e.unique_id not in known:
                    known.add(e.unique_id)
                    new.append(e)
        coordinator.provided_unique_ids |= known
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class OmadaOnlineSensor(OmadaDeviceEntity, BinarySensorEntity):
    """Device connected to the controller. Main entity of the device."""

    _attr_translation_key = "online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: OmadaCoordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "online")

    @property
    def is_on(self) -> bool:
        return self.device_online

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        d = self.ref.node.device
        return {"status": d.status, "ip": d.ip, "model": d.model, "firmware": d.firmware}


class OmadaWanBinary(OmadaDeviceEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: OmadaCoordinator, mac: str, port: int, kind: str) -> None:
        super().__init__(coordinator, mac, f"wan{port}_{kind}")
        self.port, self.kind = port, kind
        if kind == "in_use":
            self._attr_device_class = BinarySensorDeviceClass.RUNNING
        self._attr_translation_key = f"wan_{kind}"
        self._attr_translation_placeholders = {"wan": self._wan.name}

    @property
    def _wan(self):
        node = self.ref.node
        return next(w for w in node.wans if w.port == self.port)  # type: ignore[union-attr]

    @property
    def available(self) -> bool:
        node = self.ref.node if self.mac in self.coordinator.devices else None
        return (super().available and self.device_online and isinstance(node, Gateway)
                and any(w.port == self.port for w in node.wans))

    @property
    def is_on(self) -> bool | None:
        if self.kind == "in_use":
            return self._wan.in_use
        return self._wan.connected if self.kind == "link" else self._wan.internet

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        return {"role": self._wan.role} if self.kind == "in_use" else None


class _PortBinary(OmadaDeviceEntity, BinarySensorEntity):
    def __init__(self, coordinator: OmadaCoordinator, mac: str, port: int, key: str) -> None:
        super().__init__(coordinator, mac, f"port{port}_{key}")
        self.port = port
        self._attr_translation_placeholders = {"port": str(port), "name": self._p.name}

    @property
    def _p(self):
        node = self.ref.node
        return next(p for p in node.ports if p.port == self.port)  # type: ignore[union-attr]

    @property
    def available(self) -> bool:
        node = self.ref.node if self.mac in self.coordinator.devices else None
        return (super().available and self.device_online and isinstance(node, Switch)
                and any(p.port == self.port for p in node.ports))


class OmadaPortLink(_PortBinary):
    _attr_translation_key = "port_link"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: OmadaCoordinator, mac: str, port: int) -> None:
        super().__init__(coordinator, mac, port, "link")

    @property
    def is_on(self) -> bool:
        return bool(self._p.link_up)


class OmadaPoePowering(_PortBinary):
    """Port is currently delivering PoE power."""

    _attr_translation_key = "poe_powering"
    _attr_device_class = BinarySensorDeviceClass.POWER
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: OmadaCoordinator, mac: str, port: int) -> None:
        super().__init__(coordinator, mac, port, "poe_powering")

    @property
    def is_on(self) -> bool:
        return bool(self._p.poe_active)
