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
from homeassistant.util import dt as dt_util

from .api.models import AccessPoint, Gateway, Switch, Wan
from .const import TOP_WAN_WINDOWS
from .coordinator import DeviceRef, OmadaConfigEntry, OmadaCoordinator
from .entity import NewEntities, OmadaControllerEntity, OmadaDeviceEntity
from .wan_control_entities import WanControlEntity, gateway_mac

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
    if coordinator.wan_control and (gw := gateway_mac(coordinator)):
        wan_sensors: list[SensorEntity] = [WanFailoverStatusSensor(coordinator, gw)]
        for port in coordinator.wan_control.config.probes:
            wan_sensors += [WanQualitySensor(coordinator, gw, port), WanProbeSensor(coordinator, gw, port, "latency"),
                            WanProbeSensor(coordinator, gw, port, "jitter"),
                            WanProbeSensor(coordinator, gw, port, "success")]
        wan_known = coordinator.new_known_set()
        wan_known.update(e.unique_id for e in wan_sensors)
        coordinator.provided_unique_ids |= wan_known
        async_add_entities(wan_sensors)
    controller_sensor = OmadaControllerSitesSensor(coordinator)
    known = coordinator.new_known_set()
    known.add(controller_sensor.unique_id)
    coordinator.provided_unique_ids |= known
    async_add_entities([controller_sensor])

    @callback
    def _add_new() -> None:
        new = NewEntities(known)
        for mac, ref in coordinator.devices.items():
            node = ref.node
            for d in DEVICE_SENSORS:
                if d.key != "temperature" or (isinstance(node, Gateway) and node.temperature is not None):
                    new.add(f"{mac}_{d.key}", lambda d=d, mac=mac: OmadaDeviceSensor(coordinator, mac, d))
            if isinstance(node, Gateway):
                new.add(f"{mac}_active_wan", lambda mac=mac: OmadaActiveWanSensor(coordinator, mac))
                for key in TOP_WAN_WINDOWS:
                    new.add(f"{mac}_top_wan_{key}", lambda k=key, mac=mac: OmadaTopWanClientSensor(coordinator, mac, k))
                for wan in node.wans:
                    for d in WAN_SENSORS:
                        new.add(f"{mac}_wan{wan.port}_{d.key}",
                                lambda d=d, p=wan.port, mac=mac: OmadaWanSensor(coordinator, mac, p, d))
            if isinstance(node, Switch):
                for p in node.ports:
                    new.add(f"{mac}_port{p.port}_speed",
                            lambda n=p.port, mac=mac: OmadaPortSpeedSensor(coordinator, mac, n))
                    if p.poe_power_w is not None:
                        new.add(f"{mac}_port{p.port}_poe_power",
                                lambda n=p.port, mac=mac: OmadaPoePowerSensor(coordinator, mac, n))
        coordinator.provided_unique_ids |= known
        if new.items:
            async_add_entities(new.items)

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
    """Biggest internet consumer in a DPI window (last 10 min or 24 h); top 5 in attributes."""

    def __init__(self, coordinator: OmadaCoordinator, mac: str, key: str) -> None:
        super().__init__(coordinator, mac, f"top_wan_{key}")
        self.key = key
        self._attr_translation_key = f"top_wan_{key}"

    @property
    def _top(self):
        if self.mac not in self.coordinator.devices:
            return None
        return self.coordinator.top_wan.get((self.site_id, self.key))

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
        mbps = lambda b: round(b * 8 / 1_000_000 / top.window_s, 2)  # noqa: E731  average over the window
        return {
            "dpi_enabled": top.dpi_enabled,
            "window_minutes": top.window_s // 60,
            "data_until": (
                dt_util.utc_from_timestamp(top.data_until).isoformat() if top.data_until else None
            ),
            "delayed": top.delayed,
            "top_clients": [
                {"name": c.name, "mac": c.mac, "download_mb": mb(c.download_bytes),
                 "upload_mb": mb(c.upload_bytes), "total_mb": mb(c.total_bytes),
                 "rate_mbps": mbps(c.total_bytes)}
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


class WanQualitySensor(WanControlEntity, SensorEntity):
    """Quality of a WAN from its pinned probes: good / poor / offline / unknown."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["good", "poor", "offline", "unknown"]
    _attr_translation_key = "wan_quality"

    def __init__(self, coordinator: OmadaCoordinator, mac: str, port: int) -> None:
        super().__init__(coordinator, mac, f"wanctl{port}_quality")
        self.port = port
        self._attr_translation_placeholders = {"wan": self._wan_name}

    @property
    def _wan_name(self) -> str:
        w = self.ctl.wans.get(self.port)
        if w:
            return w.name
        site = self.ctl._site()
        return next((x.name for x in site.gateways[0].wans if x.port == self.port), f"WAN{self.port}") if site \
            else f"WAN{self.port}"

    @property
    def native_value(self) -> str:
        w = self.ctl.wans.get(self.port)
        return w.quality.quality if w and w.quality else "unknown"

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        w = self.ctl.wans.get(self.port)
        q = w.quality if w else None
        return {
            "probes": [{"host": r.target.host, "ip": r.target.ip, "ok": r.ok, "latency_ms": r.latency_ms,
                        "error": r.error} for r in (q.results if q else [])],
            "stable_minutes": self.ctl.stable_minutes(self.port),
            "samples": q.samples if q else 0,
            "updated": dt_util.utc_from_timestamp(q.at).isoformat() if q else None,
            "skipped_probes": self.ctl.skipped.get(self.port, []),
        }


class WanProbeSensor(WanControlEntity, SensorEntity):
    """Probe latency / jitter (ms) or success (%) of a WAN."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: OmadaCoordinator, mac: str, port: int, kind: str) -> None:
        super().__init__(coordinator, mac, f"wanctl{port}_{kind}")
        self.port, self.kind = port, kind
        self._attr_translation_key = f"wan_probe_{kind}"
        if kind == "success":
            self._attr_native_unit_of_measurement = PERCENTAGE
        else:
            self._attr_native_unit_of_measurement = UnitOfTime.MILLISECONDS
            self._attr_device_class = SensorDeviceClass.DURATION
        site = self.ctl._site()
        name = next((x.name for x in site.gateways[0].wans if x.port == port), f"WAN{port}") if site else f"WAN{port}"
        self._attr_translation_placeholders = {"wan": name}

    @property
    def native_value(self) -> float | None:
        w = self.ctl.wans.get(self.port)
        q = w.quality if w else None
        if not q:
            return None
        if self.kind == "latency":
            return q.median_ms
        if self.kind == "jitter":
            return q.jitter_ms
        return round(100 * q.ok / q.total) if q.total else None


class WanFailoverStatusSensor(WanControlEntity, SensorEntity):
    """ok / failed_over / cooldown / monitoring / error, with the failback countdown."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["ok", "failed_over", "cooldown", "monitoring", "error"]
    _attr_translation_key = "wan_failover_status"

    def __init__(self, coordinator: OmadaCoordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "wanctl_status")

    @property
    def native_value(self) -> str:
        return self.ctl.status()

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        ctl = self.ctl
        names = {p: w.name for p, w in ctl.wans.items()}
        last = ctl.last_action
        return {
            "mode": ctl.mode,
            "wan_in_use": names.get(ctl.in_use_port()),
            "preferred_wan": names.get(ctl.preferred_port),
            "preferred_stable_minutes": ctl.stable_minutes(ctl.preferred_port),
            "failback_after_minutes": ctl.config.failback_min,
            "cooldown_left_minutes": ctl.cooldown_left_min(),
            "last_action": {**last, "at": dt_util.utc_from_timestamp(last["at"]).isoformat()} if last else None,
            "error": ctl.error,
        }
