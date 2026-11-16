"""Entities of Enhanced WAN Control (quality per WAN, failover state, selects)."""

from __future__ import annotations

from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .entity import OmadaDeviceEntity
from .wan_control import SIGNAL_WAN_CONTROL, WanControl


class WanControlEntity(OmadaDeviceEntity):
    """Attached to the gateway; refreshed by the WAN control loop and by the coordinator."""

    def __init__(self, coordinator, mac: str, key: str) -> None:
        super().__init__(coordinator, mac, key)

    @property
    def ctl(self) -> WanControl:
        return self.coordinator.wan_control

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_WAN_CONTROL, self.async_write_ha_state)
        )


def gateway_mac(coordinator) -> str | None:
    ctl = coordinator.wan_control
    site = ctl._site() if ctl else None
    return site.gateways[0].device.mac if site else None
