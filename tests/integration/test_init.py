import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.open_omada.const import DOMAIN

from .conftest import AP, GW, OID, SW


async def _setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _eid(hass, platform, unique_id):
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


async def test_setup_creates_device_hierarchy(hass: HomeAssistant, fake_client, config_entry) -> None:
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    devreg = dr.async_get(hass)
    controller = devreg.async_get_device_by_identifier((DOMAIN, OID), config_entry.entry_id)
    assert controller and controller.entry_type is dr.DeviceEntryType.SERVICE
    for mac in (GW, SW, AP):
        dev = devreg.async_get_device_by_identifier((DOMAIN, mac), config_entry.entry_id)
        assert dev and dev.via_device_id == controller.id


async def test_entity_states(hass: HomeAssistant, fake_client, config_entry) -> None:
    await _setup(hass, config_entry)
    assert hass.states.get(_eid(hass, "sensor", f"{GW}_cpu")).state == "11"
    assert hass.states.get(_eid(hass, "binary_sensor", f"{GW}_online")).state == "on"
    assert hass.states.get(_eid(hass, "binary_sensor", f"{GW}_wan1_internet")).state == "on"
    dl = hass.states.get(_eid(hass, "sensor", f"{GW}_wan1_download"))
    assert float(dl.state) == 1.0 and dl.attributes["unit_of_measurement"] == "Mbit/s"  # 125 KB/s
    assert hass.states.get(_eid(hass, "switch", f"{GW}_acl_acl1")).state == "on"
    assert hass.states.get(_eid(hass, "switch", f"{GW}_policy_route_pr1")).state == "off"
    assert hass.states.get(_eid(hass, "switch", f"{SW}_port1_poe")).state == "on"
    assert hass.states.get(_eid(hass, "binary_sensor", f"{SW}_port1_link")).state == "on"
    assert hass.states.get(_eid(hass, "sensor", f"{SW}_port1_poe_power")).state == "4.5"
    pf = hass.states.get(_eid(hass, "switch", f"{GW}_port_forward_pf1"))
    assert pf.state == "on" and pf.attributes["forward_ip"] == "10.0.0.5"
    assert hass.states.get(_eid(hass, "sensor", f"{GW}_active_wan")).state == "WAN1"
    assert hass.states.get(_eid(hass, "binary_sensor", f"{GW}_wan1_in_use")).state == "on"
    for key, window in (("10m", 10), ("24h", 1440)):
        top = hass.states.get(_eid(hass, "sensor", f"{GW}_top_wan_{key}"))
        assert top.state == "Laptop" and top.attributes["window_minutes"] == window
        assert top.attributes["top_clients"][0]["total_mb"] == 3.5
    rate = hass.states.get(_eid(hass, "sensor", f"{GW}_top_wan_10m")).attributes["top_clients"][0]["rate_mbps"]
    assert rate == round(3_500_000 * 8 / 1_000_000 / 600, 2)
    ssid = hass.states.get(_eid(hass, "switch", f"{OID}_site1_ssid_s1"))
    assert ssid.state == "on" and ssid.attributes["access_points"] == ["ap1"]


async def test_ssid_switch_calls_api(hass: HomeAssistant, fake_client, config_entry) -> None:
    await _setup(hass, config_entry)
    eid = _eid(hass, "switch", f"{OID}_site1_ssid_s1")
    await hass.services.async_call("switch", "turn_off", {"entity_id": eid}, blocking=True)
    await hass.async_block_till_done()
    assert fake_client.calls == [("ssid", "site1", "s1", False)]
    assert hass.states.get(eid).state == "off"


async def test_reboot_button(hass: HomeAssistant, fake_client, config_entry) -> None:
    await _setup(hass, config_entry)
    eid = _eid(hass, "button", f"{AP}_reboot")
    await hass.services.async_call("button", "press", {"entity_id": eid}, blocking=True)
    assert fake_client.calls == [("reboot", AP)]


async def test_auth_failure_starts_reauth(hass: HomeAssistant, fake_client, config_entry) -> None:
    fake_client.fail_auth = True
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


async def test_unload(hass: HomeAssistant, fake_client, config_entry) -> None:
    await _setup(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_stale_entities_removed_but_valid_ones_kept(hass: HomeAssistant, fake_client, config_entry) -> None:
    """A stale registry entry of an online device is dropped; every provided entity survives."""
    registry = er.async_get(hass)
    stale = registry.async_get_or_create(
        "switch", DOMAIN, f"{SW}_port9_poe", config_entry=config_entry
    )
    await _setup(hass, config_entry)
    assert registry.async_get(stale.entity_id) is None
    provided = config_entry.runtime_data.provided_unique_ids
    in_registry = {e.unique_id for e in er.async_entries_for_config_entry(registry, config_entry.entry_id)}
    assert provided == in_registry
    assert _eid(hass, "sensor", f"{OID}_devices") is not None


async def test_port_switch_uses_disable_profile_and_restores(hass: HomeAssistant, fake_client, config_entry) -> None:
    await _setup(hass, config_entry)
    eid = _eid(hass, "switch", f"{SW}_port1_enabled")
    assert hass.states.get(eid).attributes["profile"] == "Trunk"
    await hass.services.async_call("switch", "turn_off", {"entity_id": eid}, blocking=True)
    await hass.async_block_till_done()
    assert fake_client.calls[-1] == ("profile", 1, "p-dis")
    assert hass.states.get(eid).state == "off"
    await hass.services.async_call("switch", "turn_on", {"entity_id": eid}, blocking=True)
    await hass.async_block_till_done()
    assert fake_client.calls[-1] == ("profile", 1, "p-trunk")  # remembered, not "All"
    assert hass.states.get(eid).state == "on"


async def test_port_switch_unknown_profile_falls_back_to_all_and_clears_status(
    hass: HomeAssistant, fake_client, config_entry
) -> None:
    fake_client.port1_profile = "p-dis"  # disabled in the Omada UI before HA ever saw it
    fake_client.port2_status = 0  # legacy status=0
    await _setup(hass, config_entry)
    config_entry.runtime_data.port_profiles.clear()
    for port in (1, 2):
        eid = _eid(hass, "switch", f"{SW}_port{port}_enabled")
        assert hass.states.get(eid).state == "off"
        await hass.services.async_call("switch", "turn_on", {"entity_id": eid}, blocking=True)
    await hass.async_block_till_done()
    assert ("profile", 1, "p-all") in fake_client.calls
    assert ("status", 2, True) in fake_client.calls
    assert ("profile", 2, "p-all") not in fake_client.calls  # already "All"


async def test_poe_off_enables_override_and_on_restores_profile(hass: HomeAssistant, fake_client, config_entry) -> None:
    await _setup(hass, config_entry)
    eid = _eid(hass, "switch", f"{SW}_port1_poe")
    await hass.services.async_call("switch", "turn_off", {"entity_id": eid}, blocking=True)
    assert fake_client.calls[-1] == ("poe_override", 1, False, "p-trunk", "net1", 0)
    assert hass.states.get(eid).state == "off"
    assert hass.states.get(eid).attributes["override_by_integration"] is True
    await hass.services.async_call("switch", "turn_on", {"entity_id": eid}, blocking=True)
    assert fake_client.calls[-2:] == [("poe_override", 1, True, "p-trunk", "net1", 0), ("follow_profile", 1, "p-trunk")]
    assert f"{SW}_1" not in config_entry.runtime_data.poe_override_ports


async def test_poe_refused_on_custom_vlan_ports(hass: HomeAssistant, fake_client, config_entry) -> None:
    fake_client.port1_tags = 2  # trunk with tagged VLAN lists
    await _setup(hass, config_entry)
    eid = _eid(hass, "switch", f"{SW}_port1_poe")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("switch", "turn_off", {"entity_id": eid}, blocking=True)
    assert not [c for c in fake_client.calls if c[0] in ("poe_override", "poe")]


async def test_disable_profile_never_remembered_when_profiles_unreadable(
    hass: HomeAssistant, fake_client, config_entry
) -> None:
    await _setup(hass, config_entry)
    coord = config_entry.runtime_data
    assert coord.port_profiles[f"{SW}_1"] == "p-trunk"
    fake_client.port1_profile = "p-dis"  # disabled in the Omada UI...
    fake_client.fail_profiles = True  # ...and /lan-profiles fails this poll
    await coord.async_refresh()
    assert coord.port_profiles[f"{SW}_1"] == "p-trunk"
    assert hass.states.get(_eid(hass, "switch", f"{SW}_port1_enabled")).state == "off"


async def test_readopted_device_gets_entities_back(hass: HomeAssistant, fake_client, config_entry) -> None:
    await _setup(hass, config_entry)
    coord = config_entry.runtime_data
    fake_client.hide_ap = True
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert _eid(hass, "button", f"{AP}_reboot") is None
    fake_client.hide_ap = False
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert _eid(hass, "button", f"{AP}_reboot") is not None


async def test_dpi_auth_error_starts_reauth(hass: HomeAssistant, fake_client, config_entry) -> None:
    fake_client.fail_dpi_auth = True
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


async def test_entities_built_once_and_ids_predicted(hass: HomeAssistant, fake_client, config_entry, caplog) -> None:
    from custom_components.open_omada import sensor

    built = 0
    original = sensor.OmadaDeviceSensor.__init__

    def counting(self, *a, **kw):
        nonlocal built
        built += 1
        original(self, *a, **kw)

    sensor.OmadaDeviceSensor.__init__ = counting
    try:
        await _setup(hass, config_entry)
        first = built
        await config_entry.runtime_data.async_refresh()
        await config_entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert first > 0 and built == first  # refreshes construct nothing new
    finally:
        sensor.OmadaDeviceSensor.__init__ = original
    assert "Unique id mismatch" not in caplog.text
