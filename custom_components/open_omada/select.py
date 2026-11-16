"""Selects: Enhanced WAN Control mode, preferred WAN and primary WAN."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .api import OmadaError
from .const import DOMAIN
from .coordinator import OmadaConfigEntry
from .wan_control import MODES
from .wan_control_entities import WanControlEntity, gateway_mac

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: OmadaConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    mac = gateway_mac(coordinator)
    if not mac:
        return
    entities = [WanModeSelect(coordinator, mac), PreferredWanSelect(coordinator, mac),
                PrimaryWanSelect(coordinator, mac)]
    known = coordinator.new_known_set()
    known.update(e.unique_id for e in entities)
    coordinator.provided_unique_ids |= known
    async_add_entities(entities)


def _wan_names(ctl) -> dict[int, str]:
    site = ctl._site()
    return {w.port: w.name for w in site.gateways[0].wans} if site else {}


class WanModeSelect(WanControlEntity, SelectEntity, RestoreEntity):
    """off = measure only, notify = events only (dry run), automatic = swap primary/backup."""

    _attr_translation_key = "wan_failover_mode"
    _attr_options = list(MODES)
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "wanctl_mode")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in MODES:
            self.ctl.mode = last.state

    @property
    def current_option(self) -> str:
        return self.ctl.mode

    async def async_select_option(self, option: str) -> None:
        self.ctl.mode = option
        self.async_write_ha_state()


class PreferredWanSelect(WanControlEntity, SelectEntity, RestoreEntity):
    """The WAN to return to after a failover (once it is stable for the failback time)."""

    _attr_translation_key = "wan_preferred"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "wanctl_preferred")

    @property
    def options(self) -> list[str]:
        return list(_wan_names(self.ctl).values())

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        names = {v: k for k, v in _wan_names(self.ctl).items()}
        if last and last.state in names:
            self.ctl.preferred_port = names[last.state]

    @property
    def current_option(self) -> str | None:
        return _wan_names(self.ctl).get(self.ctl.preferred_port)

    async def async_select_option(self, option: str) -> None:
        names = {v: k for k, v in _wan_names(self.ctl).items()}
        self.ctl.preferred_port = names[option]
        self.async_write_ha_state()


class PrimaryWanSelect(WanControlEntity, SelectEntity):
    """Manual failover: makes the chosen WAN the primary (the other becomes the backup)."""

    _attr_translation_key = "wan_primary"

    def __init__(self, coordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "wanctl_primary")

    @property
    def options(self) -> list[str]:
        return list(_wan_names(self.ctl).values())

    @property
    def current_option(self) -> str | None:
        return _wan_names(self.ctl).get(self.ctl.primary_port())

    async def async_select_option(self, option: str) -> None:
        names = {v: k for k, v in _wan_names(self.ctl).items()}
        try:
            await self.ctl.async_set_primary(names[option])
        except OmadaError as err:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="action_failed",
                                     translation_placeholders={"error": str(err)}) from err
