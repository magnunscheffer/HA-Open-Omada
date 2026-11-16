"""Base entities for Open Omada."""

from __future__ import annotations

from collections.abc import Awaitable

from homeassistant.const import CONF_URL
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceEntryType,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import OmadaError
from .const import DOMAIN, MANUFACTURER
from .coordinator import DeviceRef, OmadaCoordinator


class OmadaEntity(CoordinatorEntity[OmadaCoordinator]):
    """Common base: naming, translated action errors."""

    _attr_has_entity_name = True
    # State accepted by the controller but not yet confirmed by a refresh.
    _optimistic: bool | None = None

    def _handle_coordinator_update(self) -> None:
        self._optimistic = None
        super()._handle_coordinator_update()

    async def _call(self, action: Awaitable[None], optimistic: bool | None = None) -> None:
        """Run a write call, surface API errors to the UI, show the new state, then refresh."""
        try:
            await action
        except OmadaError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="action_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        if optimistic is not None:
            self._optimistic = optimistic
            self.async_write_ha_state()
        await self.coordinator.async_request_refresh()


class OmadaControllerEntity(OmadaEntity):
    """Entity attached to the Omada Controller (service) device."""

    def __init__(self, coordinator: OmadaCoordinator, key: str) -> None:
        super().__init__(coordinator)
        snap = coordinator.data
        self._attr_unique_id = f"{snap.omadac_id}_{key}"
        self._attr_device_info = controller_device_info(coordinator)


def controller_device_info(coordinator: OmadaCoordinator) -> DeviceInfo:
    snap = coordinator.data
    return DeviceInfo(
        identifiers={(DOMAIN, snap.omadac_id)},
        entry_type=DeviceEntryType.SERVICE,
        manufacturer=MANUFACTURER,
        model="Omada SDN Controller",
        name="Omada Controller",
        sw_version=snap.version,
        configuration_url=coordinator.config_entry.data[CONF_URL],
    )


class OmadaDeviceEntity(OmadaEntity):
    """Entity attached to a physical Omada device (gateway, switch, AP)."""

    def __init__(self, coordinator: OmadaCoordinator, mac: str, key: str) -> None:
        super().__init__(coordinator)
        self.mac = mac
        self._attr_unique_id = f"{mac}_{key}"
        dev = self.ref.node.device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            connections={(CONNECTION_NETWORK_MAC, format_mac(mac.replace("-", ":")))},
            manufacturer=MANUFACTURER,
            model=dev.model,
            name=dev.name,
            sw_version=dev.firmware,
            serial_number=dev.serial,
            via_device_id=coordinator.controller_device_id,
            configuration_url=coordinator.config_entry.data[CONF_URL],
        )

    @property
    def ref(self) -> DeviceRef:
        return self.coordinator.devices[self.mac]

    @property
    def site_id(self) -> str:
        return self.ref.site.site.id

    @property
    def available(self) -> bool:
        return super().available and self.mac in self.coordinator.devices

    @property
    def device_online(self) -> bool:
        return self.mac in self.coordinator.devices and self.ref.node.device.online


class OmadaGatewayChild(OmadaDeviceEntity):
    """Site-level gateway feature (ACL, policy route) shown on the site's gateway device."""

    def __init__(self, coordinator: OmadaCoordinator, site_id: str, key: str) -> None:
        site = next(s for s in coordinator.data.sites if s.site.id == site_id)
        super().__init__(coordinator, site.gateways[0].device.mac, key)
