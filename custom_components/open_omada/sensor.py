"""Sensors: device CPU/memory/uptime, WAN metrics, switch port speed, controller."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfDataRate,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.models import AccessPoint, Gateway, Switch, Wan
from .coordinator import DeviceRef, OmadaConfigEntry, OmadaCoordinator
from .entity import OmadaControllerEntity, OmadaDeviceEntity

# Coordinator centralises updates; read-only platform.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class DeviceSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Gateway | Switch | AccessPoint], float | int | str | None]


DEVICE_SENSORS: tuple[DeviceSensorDescription, ...] = (
    DeviceSensorDescription(
        key="cpu",
        translation_key="cpu",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda n: n.cpu,
    ),
    DeviceSensorDescription(
        key="memory",
        translation_key="memory",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda n: n.mem,
    ),
    DeviceSensorDescription(
        key="uptime",
        translation_key="uptime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.DAYS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda n: n.device.uptime_seconds,
    ),
    DeviceSensorDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement="°C",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda n: n.temperature if isinstance(n, Gateway) else None,
    ),
)


@dataclass(frozen=True, kw_only=True)
class WanSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Wan], float | int | str | None]


WAN_SENSORS: tuple[WanSensorDescription, ...] = (
    WanSensorDescription(
        key="download",
        translation_key="wan_download",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.KILOBITS_PER_SECOND,
        suggested_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda w: w.rx_rate_kbps,
    ),
    WanSensorDescription(
        key="upload",
        translation_key="wan_upload",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.KILOBITS_PER_SECOND,
        suggested_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda w: w.tx_rate_kbps,
    ),
    WanSensorDescription(
        key="ip",
        translation_key="wan_ip",
        value_fn=lambda w: w.ip,
    ),
    WanSensorDescription(
        key="latency",
        translation_key="wan_latency",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda w: w.latency_ms,
    ),
    WanSensorDescription(
        key="loss",
        translation_key="wan_loss",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda w: w.loss_pct,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OmadaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    controller_sensor = OmadaControllerSitesSensor(coordinator)
    known: set[str] = {controller_sensor.unique_id}
    coordinator.provided_unique_ids |= known
    async_add_entities([controller_sensor])

    @callback
    def _add_new() -> None:
        new: list[SensorEntity] = []
        for mac, ref in coordinator.devices.items():
            candidates: list[SensorEntity] = [
                OmadaDeviceSensor(coordinator, mac, d)
                for d in DEVICE_SENSORS
                if d.key != "temperature" or (isinstance(ref.node, Gateway) and ref.node.temperature is not None)
            ]
            if isinstance(ref.node, Gateway):
                candidates.append(OmadaActiveWanSensor(coordinator, mac))
                candidates.append(OmadaTopWanClientSensor(coordinator, mac))
                for wan in ref.node.wans:
                    candidates += [OmadaWanSensor(coordinator, mac, wan.port, d) for d in WAN_SENSORS]
            if isinstance(ref.node, Switch):
                candidates += [OmadaPortSpeedSensor(coordinator, mac, p.port) for p in ref.node.ports]
                candidates += [
                    OmadaPoePowerSensor(coordinator, mac, p.port) for p in ref.node.ports if p.poe_power_w is not None
                ]
            for entity in candidates:
                if entity.unique_id not in known:
                    known.add(entity.unique_id)
                    new.append(entity)
        coordinator.provided_unique_ids |= known
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class OmadaControllerSitesSensor(OmadaControllerEntity, SensorEntity):
    """Number of devices managed by the controller (all sites)."""

    _attr_translation_key = "devices"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: OmadaCoordinator) -> None:
        super().__init__(coordinator, "devices")

    @property
    def native_value(self) -> int:
        return sum(len(s.devices) for s in self.coordinator.data.sites)

    @property
    def extra_state_attributes(self) -> dict[str, list[str]]:
        return {"sites": [s.site.name for s in self.coordinator.data.sites]}


class OmadaDeviceSensor(OmadaDeviceEntity, SensorEntity):
    entity_description: DeviceSensorDescription

    def __init__(self, coordinator: OmadaCoordinator, mac: str, description: DeviceSensorDescription) -> None:
        super().__init__(coordinator, mac, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return super().available and self.device_online

    @property
    def native_value(self) -> float | int | str | None:
        return self.entity_description.value_fn(self.ref.node)


class OmadaWanSensor(OmadaDeviceEntity, SensorEntity):
    entity_description: WanSensorDescription

    def __init__(self, coordinator: OmadaCoordinator, mac: str, port: int, description: WanSensorDescription) -> None:
        super().__init__(coordinator, mac, f"wan{port}_{description.key}")
        self.entity_description = description
        self.port = port
        self._attr_translation_placeholders = {"wan": _wan(self.ref, port).name}

    @property
    def available(self) -> bool:
        return super().available and self.device_online and _wan(self.ref, self.port) is not None

    @property
    def native_value(self) -> float | int | str | None:
        wan = _wan(self.ref, self.port)
        return self.entity_description.value_fn(wan) if wan else None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        if self.entity_description.key != "ip" or not (wan := _wan(self.ref, self.port)):
            return None
        return {"public": wan.ip_is_public, "protocol": wan.proto}


class OmadaPortSpeedSensor(OmadaDeviceEntity, SensorEntity):
    _attr_translation_key = "port_speed"
    _attr_device_class = SensorDeviceClass.DATA_RATE
    _attr_native_unit_of_measurement = UnitOfDataRate.MEGABITS_PER_SECOND
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: OmadaCoordinator, mac: str, port: int) -> None:
        super().__init__(coordinator, mac, f"port{port}_speed")
        self.port = port
        self._attr_translation_placeholders = {"port": str(port), "name": _port(self.ref, port).name}

    @property
    def available(self) -> bool:
        return super().available and self.device_online

    @property
    def native_value(self) -> int | None:
        p = _port(self.ref, self.port)
        return p.speed_mbps if p and p.link_up else 0


class OmadaActiveWanSensor(OmadaDeviceEntity, SensorEntity):
    """Which WAN(s) carry traffic now; changes when failover kicks in."""

    _attr_translation_key = "active_wan"

    def __init__(self, coordinator: OmadaCoordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "active_wan")

    @property
    def available(self) -> bool:
        return super().available and self.device_online

    @property
    def native_value(self) -> str:
        node = self.ref.node
        active = [w.name for w in node.wans if w.in_use] if isinstance(node, Gateway) else []
        return ", ".join(active) if active else "none"

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        node = self.ref.node
        wans = node.wans if isinstance(node, Gateway) else []
        return {
            "mode": self.ref.site.wan_mode,
            "primary": [w.name for w in wans if w.role == "primary"],
            "backup": [w.name for w in wans if w.role == "backup"],
        }


class OmadaTopWanClientSensor(OmadaDeviceEntity, SensorEntity):
    """Biggest internet consumer in the last hour; top 5 in attributes (gateway DPI)."""

    _attr_translation_key = "top_wan_client"

    def __init__(self, coordinator: OmadaCoordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "top_wan_client")

    @property
    def _top(self):
        return self.coordinator.top_wan.get(self.site_id) if self.mac in self.coordinator.devices else None

    @property
    def available(self) -> bool:
        return super().available and self._top is not None

    @property
    def native_value(self) -> str | None:
        top = self._top
        if top is None or not top.dpi_enabled:
            return None
        return top.clients[0].name if top.clients else "none"

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        top = self._top
        if top is None:
            return {}
        mb = lambda b: round(b / 1_000_000, 1)  # noqa: E731
        return {
            "dpi_enabled": top.dpi_enabled,
            "window_minutes": top.window_s // 60,
            "top_clients": [
                {"name": c.name, "mac": c.mac, "download_mb": mb(c.download_bytes),
                 "upload_mb": mb(c.upload_bytes), "total_mb": mb(c.total_bytes), "top_app": c.top_app}
                for c in top.clients
            ],
        }


class OmadaPoePowerSensor(OmadaDeviceEntity, SensorEntity):
    """Current PoE power drawn on a switch port."""

    _attr_translation_key = "poe_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: OmadaCoordinator, mac: str, port: int) -> None:
        super().__init__(coordinator, mac, f"port{port}_poe_power")
        self.port = port
        self._attr_translation_placeholders = {"port": str(port), "name": _port(self.ref, port).name}

    @property
    def available(self) -> bool:
        p = _port(self.ref, self.port) if super().available and self.device_online else None
        return p is not None and p.poe_power_w is not None

    @property
    def native_value(self) -> float | None:
        p = _port(self.ref, self.port)
        return p.poe_power_w if p else None


def _wan(ref: DeviceRef, port: int) -> Wan | None:
    if isinstance(ref.node, Gateway):
        return next((w for w in ref.node.wans if w.port == port), None)
    return None


def _port(ref: DeviceRef, port: int):
    if isinstance(ref.node, Switch):
        return next((p for p in ref.node.ports if p.port == port), None)
    return None
