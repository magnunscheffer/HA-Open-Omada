"""Reboot button for every Omada device."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import OmadaConfigEntry, OmadaCoordinator
from .entity import OmadaDeviceEntity

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
        new = [OmadaRebootButton(coordinator, mac) for mac in coordinator.devices if f"{mac}_reboot" not in known]
        known.update(f"{mac}_reboot" for mac in coordinator.devices)
        coordinator.provided_unique_ids |= known
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class OmadaRebootButton(OmadaDeviceEntity, ButtonEntity):
    _attr_device_class = ButtonDeviceClass.RESTART

    def __init__(self, coordinator: OmadaCoordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "reboot")

    @property
    def available(self) -> bool:
        return super().available and self.device_online

    async def async_press(self) -> None:
        await self._call(self.coordinator.client.reboot_device(self.site_id, self.mac))
