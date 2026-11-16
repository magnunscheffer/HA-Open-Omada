"""Enhanced WAN Control with the fake controller (2 WANs, failover WAN1 primary / WAN2 backup)."""

from unittest.mock import patch

import pytest
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.open_omada.api.wanprobe import GOOD, OFFLINE, POOR, WanQuality
from custom_components.open_omada.const import DOMAIN

from .conftest import ENTRY_DATA, GW, OID

OPTIONS = {"wan_control": True, "probes_wan1": "1.1.1.1, 192.0.2.10", "probes_wan2": "1.0.0.1, 192.0.2.20",
           "latency_ms": 150, "interval_s": 30, "confirm_tests": 2, "confirm_interval_s": 5,
           "cooldown_min": 15, "failback_min": 60}


def _form(opts: dict) -> dict:
    """The options form groups the numbers in the 'quality' and 'failover' sections."""
    quality = ("latency_ms", "interval_s", "confirm_tests", "confirm_interval_s")
    failover = ("cooldown_min", "failback_min")
    top = {k: v for k, v in opts.items() if k not in quality + failover}
    return {**top, "quality": {k: opts[k] for k in quality}, "failover": {k: opts[k] for k in failover}}


@pytest.fixture
async def setup(hass: HomeAssistant, fake_client):
    fake_client.two_wans = True
    quality = {1: GOOD, 2: GOOD}

    async def fake_measure(targets, **kw):
        port = 1 if targets[0].ip in ("1.1.1.1", "192.0.2.10") else 2
        q = quality[port]
        return WanQuality(q, 30.0 if q == GOOD else 400.0, 2.0, 2 if q == GOOD else 0, 2, 1, 0.0, [])

    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, options=OPTIONS, unique_id=OID, title="Omada")
    entry.add_to_hass(hass)
    with patch("custom_components.open_omada.wan_control.measure_wan", fake_measure), \
         patch("homeassistant.components.network.async_get_source_ip", return_value="192.0.2.50"), \
         patch("custom_components.open_omada.wan_control.async_track_time_interval"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry, entry.runtime_data.wan_control, quality


def _eid(hass, platform, uid):
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, uid)


async def test_objects_created_and_pinned_routes_on_top(hass, fake_client, setup):
    entry, ctl, _ = setup
    names = {name: ips for name, ips in fake_client.groups.values()}
    tag = ctl.tag
    assert names[f"Open Omada probe {tag} HA"] == ["192.0.2.50"]
    assert names[f"Open Omada probe {tag} WAN1"] == ["1.1.1.1", "192.0.2.10"]
    assert names[f"Open Omada probe {tag} WAN2"] == ["1.0.0.1", "192.0.2.20"]
    created = [c for c in fake_client.calls if c[0] == "create_route"]
    assert ("create_route", f"Open Omada probe {tag} WAN1", "1_aaa") in created
    assert ("create_route", f"Open Omada probe {tag} WAN2", "2_bbb") in created
    order = next(c for c in fake_client.calls if c[0] == "order")[1]
    assert order[-1] == "pr1"  # the user's own route comes after the probe routes
    assert ctl.preferred_port == 1


async def test_entities(hass, fake_client, setup):
    entry, ctl, _ = setup
    await ctl._async_cycle()
    await hass.async_block_till_done()
    assert hass.states.get(_eid(hass, "sensor", f"{GW}_wanctl1_quality")).state == "good"
    assert hass.states.get(_eid(hass, "sensor", f"{GW}_wanctl2_latency")).state == "30.0"
    assert hass.states.get(_eid(hass, "select", f"{GW}_wanctl_mode")).state == "notify"
    assert hass.states.get(_eid(hass, "select", f"{GW}_wanctl_primary")).state == "WAN1"


async def test_notify_mode_suggests_but_does_not_switch(hass, fake_client, setup):
    entry, ctl, quality = setup
    events = []
    hass.bus.async_listen(f"{DOMAIN}_wan_failover", lambda e: events.append(e.data))
    quality[1] = POOR
    await ctl._async_cycle()
    await ctl._async_cycle()  # same condition: notified once
    await hass.async_block_till_done()
    assert [e["dry_run"] for e in events] == [True]
    assert not [c for c in fake_client.calls if c[0] == "set_primary"]
    notes = persistent_notification._async_get_or_create_notifications(hass)
    note = notes[f"{DOMAIN}_wan_failover_{entry.entry_id}"]
    assert "WAN1" in note["message"] and "WAN2" in note["message"]
    assert len([n for n in notes if n.startswith(f"{DOMAIN}_wan_failover")]) == 1


async def test_automatic_failover_then_failback_after_stable_hour(hass, fake_client, setup):
    entry, ctl, quality = setup
    ctl.mode = "automatic"
    quality[1] = OFFLINE
    await ctl._async_cycle()
    assert ("set_primary", "2_bbb") in fake_client.calls
    await config_refresh(hass, entry)
    assert ctl.primary_port() == 2

    # WAN1 recovers: no failback before 60 min of continuous Good, and not inside the cooldown.
    quality[1] = GOOD
    ctl.last_switch_at -= 16 * 60  # cooldown over
    await ctl._async_cycle()
    assert fake_client.calls.count(("set_primary", "1_aaa")) == 0
    # a Poor result restarts the count
    ctl.wans[1].good_since -= 59 * 60
    quality[1] = POOR
    await ctl._async_cycle()
    assert ctl.wans[1].good_since is None
    quality[1] = GOOD
    await ctl._async_cycle()
    ctl.wans[1].good_since -= 61 * 60
    await ctl._async_cycle()
    assert ("set_primary", "1_aaa") in fake_client.calls


async def test_no_switch_when_the_other_wan_is_not_good(hass, fake_client, setup):
    entry, ctl, quality = setup
    ctl.mode = "automatic"
    quality[1], quality[2] = POOR, POOR
    await ctl._async_cycle()
    assert not [c for c in fake_client.calls if c[0] == "set_primary"]


async def test_cooldown_blocks_a_second_switch(hass, fake_client, setup):
    entry, ctl, quality = setup
    ctl.mode = "automatic"
    quality[1] = POOR
    await ctl._async_cycle()
    await config_refresh(hass, entry)
    quality[1], quality[2] = GOOD, POOR  # now the new active WAN is bad
    await ctl._async_cycle()
    assert [c for c in fake_client.calls if c[0] == "set_primary"] == [("set_primary", "2_bbb")]


async def test_turning_the_feature_off_removes_the_objects(hass, fake_client, setup):
    entry, ctl, _ = setup
    with patch("homeassistant.components.network.async_get_source_ip", return_value="192.0.2.50"):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], _form({**OPTIONS, "wan_control": False}))
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert not fake_client.groups
    assert all(not r["name"].startswith("Open Omada probe") for r in fake_client.routes)


async def test_options_reject_the_same_probe_on_both_wans(hass, fake_client, setup):
    entry, ctl, _ = setup
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _form({**OPTIONS, "probes_wan2": "1.1.1.1"}))
    assert result["errors"] == {"base": "probes_overlap"}


async def config_refresh(hass, entry):
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_probe_on_a_wan_dns_server_is_skipped(hass, fake_client, setup):
    """The gateway routes its WAN DNS servers itself, so such a probe cannot be pinned."""
    entry, ctl, _ = setup
    fake_client.wan_dns = {"1.1.1.1", "8.8.8.8"}
    await ctl._sync_objects()
    names = {name: ips for name, ips in fake_client.groups.values()}
    assert names[f"Open Omada probe {ctl.tag} WAN1"] == ["192.0.2.10"]
    assert [t.ip for t in ctl.wans[1].targets] == ["192.0.2.10"]
    assert ctl.skipped == {1: ["1.1.1.1"]}
    await ctl._async_cycle()
    await hass.async_block_till_done()
    state = hass.states.get(_eid(hass, "sensor", f"{GW}_wanctl1_quality"))
    assert state.attributes["skipped_probes"] == ["1.1.1.1"]


async def test_probe_routes_are_not_exposed_as_switches(hass, fake_client, setup):
    entry, ctl, _ = setup
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    uids = {e.unique_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}
    assert f"{GW}_policy_route_pr1" in uids
    probe_ids = [r["id"] for r in fake_client.routes]
    assert probe_ids and not any(f"{GW}_policy_route_{rid}" in uids for rid in probe_ids)


async def test_options_form_sections_names_and_flat_storage(hass, fake_client, setup):
    entry, ctl, _ = setup
    fake_client.wan_dns = {"192.0.2.53"}
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["description_placeholders"] == {"wan1": "WAN1", "wan2": "WAN2", "wan_dns": "192.0.2.53"}
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], _form({**OPTIONS, "latency_ms": 200, "failback_min": 30}))
    assert result["type"] == "create_entry"
    assert entry.options["latency_ms"] == 200 and entry.options["failback_min"] == 30
    assert "quality" not in entry.options and "failover" not in entry.options


async def test_last_switch_sensor_records_automatic_and_manual(hass, fake_client, setup):
    entry, ctl, quality = setup
    eid = _eid(hass, "sensor", f"{GW}_wanctl_last_switch")
    assert hass.states.get(eid).state == "unknown"
    ctl.mode = "automatic"
    quality[1] = POOR
    await ctl._async_cycle()
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert state.state != "unknown"
    attrs = state.attributes
    assert (attrs["from"], attrs["to"], attrs["trigger"]) == ("WAN1", "WAN2", "automatic")
    ctl.last_switch_at = None  # skip the cooldown for the manual test
    await hass.services.async_call("select", "select_option",
                                   {"entity_id": _eid(hass, "select", f"{GW}_wanctl_primary"), "option": "WAN1"},
                                   blocking=True)
    await hass.async_block_till_done()
    state = hass.states.get(eid)
    assert (state.attributes["to"], state.attributes["trigger"]) == ("WAN1", "manual")


async def test_few_usable_probes_raise_a_repair_issue(hass, fake_client, setup):
    from homeassistant.helpers import issue_registry as ir

    entry, ctl, _ = setup
    issues = ir.async_get(hass)
    # the fixture has 2 probes per WAN: below the recommended 3
    assert issues.async_get_issue(DOMAIN, f"few_probes_{entry.entry_id}_1")
    ctl.config.probes[1] = ["192.0.2.10", "192.0.2.11", "192.0.2.12"]
    await ctl._sync_objects()
    assert not issues.async_get_issue(DOMAIN, f"few_probes_{entry.entry_id}_1")
    assert issues.async_get_issue(DOMAIN, f"few_probes_{entry.entry_id}_2")


async def test_options_reject_more_than_ten_probes(hass, fake_client, setup):
    entry, ctl, _ = setup
    many = ", ".join(f"192.0.2.{i}" for i in range(100, 111))
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _form({**OPTIONS, "probes_wan1": many}))
    assert result["errors"] == {"base": "probes_too_many"}
