"""Fixtures for Home Assistant integration tests (run with .venv-ha)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.open_omada.api.exceptions import OmadaAuthError  # noqa: E402
from custom_components.open_omada.const import DOMAIN  # noqa: E402

OID = "omadac-test"
SITE = "site1"
GW, SW, AP = "AA-00-00-00-00-01", "AA-00-00-00-00-02", "AA-00-00-00-00-03"
ENTRY_DATA = {"url": "https://omada.test:8043", "client_id": "cid", "client_secret": "sec", "verify_ssl": False}


class FakeClient:
    """Synthetic controller: 1 site, 1 gateway, 1 PoE switch, 1 AP, 1 SSID."""

    def __init__(self) -> None:
        self.fail_auth = False
        self.calls: list[tuple] = []
        self.ssid_enabled = True
        self.port1_profile = "p-trunk"
        self.port1_tags = 0
        self.port1_poe = 1
        self.port1_override = False
        self.acl_live_description = "Deny-DMZ"
        self.fail_profiles = False
        self.dpi_empty = False
        self.two_wans = False
        self.primary, self.backup = "1_aaa", "2_bbb"
        self.groups: dict = {}
        self.routes: list = []
        self.wan_dns: set = set()
        self.detection: dict = {}  # port -> onlineDetection (1/0), omitted when absent
        self.fail_dpi_auth = False
        self.hide_ap = False
        self.port2_status = 1
        self.controller_info: dict = {}

    async def get_controller_info(self):
        self.controller_info = {"omadacId": OID, "controllerVer": "6.3.0.45"}
        return self.controller_info

    async def authenticate(self):
        if self.fail_auth:
            raise OmadaAuthError("bad")

    async def get_sites(self):
        if self.fail_auth:
            raise OmadaAuthError("bad")
        return [{"siteId": SITE, "name": "Home"}]

    async def get_devices(self, site):
        base = {"status": 1, "firmwareVersion": "1.0", "cpuUtil": 5, "memUtil": 40, "uptime": "1day(s) 2h 3m 4s",
                "ip": "192.168.77.10", "publicIp": "203.0.113.7", "sn": "SNSECRET123",
                "uplinkDeviceMac": "AA-00-00-00-00-09"}
        return [
            {**base, "mac": GW, "name": "fw1.lab.example", "type": "gateway", "model": "ER605 v2.0"},
            {**base, "mac": SW, "name": "sw1", "type": "switch", "subtype": "smart", "model": "SG2218P"},
            {**base, "mac": AP, "name": "ap1", "type": "ap", "model": "EAP670"},
        ][: 2 if self.hide_ap else 3]

    async def get_gateway(self, site, mac):
        return {"cpuUtil": 11, "memUtil": 38}

    async def get_gateway_wan_status(self, site, mac):
        wans = [{"port": 1, "name": "WAN1", "mode": 0, "status": 1, "internetState": 1, "ip": "8.8.4.4",
                 "proto": "pppoe", "rxRate": 125, "txRate": 25, "latency": 10, "loss": 0.0}]
        if self.two_wans:
            wans.append({"port": 2, "name": "WAN2", "mode": 0, "status": 1, "internetState": 1, "ip": "203.0.113.9",
                         "proto": "dhcp", "rxRate": 10, "txRate": 1, "latency": 20, "loss": 0.0})
        for w in wans:
            if w["port"] in self.detection:
                w["onlineDetection"] = self.detection[w["port"]]
        return wans

    async def get_switch(self, site, mac, agile=False):
        return {"cpuUtil": 20, "memUtil": 54, "portList": [
            {"port": 1, "name": "FW1", "status": 1, "poeMode": self.port1_poe, "lagPort": False,
             "profileId": self.port1_profile, "profileName": "Trunk"},
            {"port": 2, "name": "P2", "status": self.port2_status, "poeMode": 0, "lagPort": False,
             "profileId": "p-all", "profileName": "All"}]}

    async def get_switch_ports_overview(self, site):
        return [{"switchMac": SW, "port": 1, "nativeNetworkId": "net1", "networkTagsSetting": self.port1_tags},
                {"switchMac": SW, "port": 2, "nativeNetworkId": "net1", "networkTagsSetting": 0}]

    async def set_switch_port_poe_via_override(self, site, mac, port, on, *, port_name, profile_id,
                                               native_network_id, network_tags_setting):
        self.calls.append(("poe_override", port, on, profile_id, native_network_id, network_tags_setting))
        if port == 1:
            self.port1_poe = int(on)
            self.port1_override = True

    async def set_switch_port_follow_profile(self, site, mac, port, *, port_name, profile_id):
        self.calls.append(("follow_profile", port, profile_id))
        if port == 1:
            self.port1_override = False

    async def get_switch_port_live(self, site, mac, port):
        if mac != SW:
            return None
        if port == 1:
            return {"switchMac": SW, "port": 1, "portName": "FW1", "profileId": self.port1_profile,
                    "profileOverrideEnable": self.port1_override, "nativeNetworkId": "net1",
                    "networkTagsSetting": self.port1_tags}
        return {"switchMac": SW, "port": 2, "portName": "P2", "profileId": "p-all",
                "profileOverrideEnable": False, "nativeNetworkId": "net1", "networkTagsSetting": 0}

    async def get_rule_live(self, site, kind, rule_id):
        rows = {"acl": await self.get_gateway_acls(site), "policy_route": await self.get_policy_routes(site),
                "port_forward": await self.get_port_forwardings(site), "vpn": await self.get_vpns(site)}[kind]
        row = next((r for r in rows if r["id"] == rule_id), None)
        return {**row, "description": self.acl_live_description} if row and kind == "acl" else row

    async def set_gateway_acl_enabled(self, site, acl, on):
        self.calls.append(("acl", acl.get("description"), on))

    async def get_lan_networks(self, site):
        return [{"id": "net1", "name": "LAN", "vlan": 1}]

    async def get_gateway_port_stats(self, site, gateway_mac, network):
        return [{"port": 4, "name": "LAN4", "mode": 1, "status": 1, "speed": 3, "poe": False, "_supportPoe": False},
                {"port": 5, "name": "LAN5", "mode": 1, "status": 0, "speed": 1, "poe": False, "_supportPoe": False},
                {"port": 1, "name": "WAN1", "mode": 0, "status": 1}]

    async def get_vpns(self, site):
        return [{"id": "vpn1", "name": "Office", "status": True}]

    async def set_vpn_enabled(self, site, vpn, on):
        self.calls.append(("vpn", vpn["id"], on))

    async def set_client_name(self, site, mac, name):
        self.calls.append(("rename", site, mac, name))

    async def set_wan_internet(self, site, gateway_mac, port, on):
        self.calls.append(("wan_internet", port, on))

    async def get_lan_profiles(self, site):
        if self.fail_profiles:
            from custom_components.open_omada.api.exceptions import OmadaApiError
            raise OmadaApiError(-1, "boom", "/lan-profiles")
        return [{"id": "p-all", "name": "All", "type": 0}, {"id": "p-dis", "name": "Disable", "type": 1},
                {"id": "p-trunk", "name": "Trunk", "type": 2}]

    async def set_switch_port_profile(self, site, mac, port, profile_id, *, port_name, profile_override):
        self.calls.append(("profile", port, profile_id))
        if port == 1:
            self.port1_profile = profile_id

    async def set_switch_port_status(self, site, mac, port, on):
        self.calls.append(("status", port, on))
        if port == 2:
            self.port2_status = int(on)

    async def get_switch_stat(self, site, mac):
        return {"supportPoe": True, "ports": [
            {"port": 1, "type": 1, "portStatus": {"linkStatus": 1, "linkSpeed": 3, "poe": True}}]}

    async def get_switch_ports_poe(self, site):
        return [{"switchMac": SW, "port": 1, "supportPoe": True, "power": 4.5}]

    async def get_ap(self, site, mac):
        return {"cpuUtil": 6, "memUtil": 68, "wlanId": "w1"}

    async def get_gateway_acls(self, site):
        return [{"id": "acl1", "index": 1, "description": "Deny-DMZ", "status": True, "policy": 0}]

    async def get_policy_routes(self, site):
        return [{"id": "pr1", "index": 1, "name": "Lab-VIVO", "status": False}, *self.routes]

    async def get_port_forwardings(self, site):
        return [{"id": "pf1", "name": "RDS", "status": True, "externalPort": "3389", "forwardIp": "10.0.0.5",
                 "forwardPort": "3389", "protocol": 0, "from": 0, "dMZ": False, "site": "x", "featureDescription": []}]

    async def set_port_forwarding_enabled(self, site, rule, on):
        self.calls.append(("port_forward", rule["id"], on))

    async def get_wan_load_balance(self, site):
        if not self.two_wans:
            return {"linkBackup": False}
        return {"linkBackup": True, "primaryWans": [self.primary], "backupWan": self.backup, "mode": 0}

    async def set_primary_wan(self, site, wan_id):
        self.calls.append(("set_primary", wan_id))
        if wan_id != self.primary:
            self.primary, self.backup = wan_id, self.primary

    async def get_wan_dns_servers(self, site):
        return set(self.wan_dns)

    async def get_ip_groups(self, site):
        return [{"groupId": gid, "name": name, "ipList": ips} for gid, (name, ips) in self.groups.items()]

    async def create_ip_group(self, site, name, ips):
        gid = f"g{len(self.groups) + 1}"
        self.groups[gid] = (name, ips)
        self.calls.append(("create_group", name, tuple(ips)))
        return gid

    async def update_ip_group(self, site, gid, name, ips):
        self.groups[gid] = (name, ips)
        self.calls.append(("update_group", name, tuple(ips)))

    async def delete_ip_group(self, site, gid):
        self.groups.pop(gid, None)
        self.calls.append(("delete_group", gid))

    async def create_probe_route(self, site, name, wan_id, source, dest):
        rid = f"r{len(self.routes) + 10}"
        self.routes.append({"id": rid, "name": name, "index": len(self.routes) + 1, "interfaceId": wan_id})
        self.calls.append(("create_route", name, wan_id))

    async def update_probe_route(self, site, rid, name, wan_id, source, dest):
        self.calls.append(("update_route", name, wan_id))

    async def delete_policy_route(self, site, rid):
        self.routes = [r for r in self.routes if r["id"] != rid]
        self.calls.append(("delete_route", rid))

    async def set_policy_route_order(self, site, order):
        index = {rid: i + 1 for i, rid in enumerate(order)}
        for r in self.routes:
            r["index"] = index.get(r["id"], r["index"])
        self.calls.append(("order", tuple(order)))

    async def get_dpi_enabled(self, site):
        if self.fail_dpi_auth:
            raise OmadaAuthError("dpi auth")
        return True

    async def get_client_wan_traffic(self, site, start, end):
        if self.dpi_empty:
            return []
        return [{"mac": "CC", "clientName": "Laptop", "download": 3_000_000, "upload": 500_000}]

    async def get_wlan_groups(self, site):
        return [{"wlanId": "w1", "name": "Default"}]

    async def get_ssids(self, site, wlan):
        return [{"id": "s1", "name": "Home-WiFi", "ssidEnable": self.ssid_enabled, "band": 3}]

    async def set_ssid_enabled(self, site, ssid, on):
        self.calls.append(("ssid", site, ssid, on))
        self.ssid_enabled = on

    async def set_switch_port_poe(self, site, mac, port, on):
        self.calls.append(("poe", mac, port, on))

    async def find_client(self, mac):
        mac = mac.replace(":", "-").upper()
        return (SITE, {"mac": mac, "name": "Phone"}) if mac == "11-22-33-44-55-66" else None

    async def reconnect_client(self, site, mac):
        self.calls.append(("reconnect", site, mac))

    async def block_client(self, site, mac):
        self.calls.append(("block", site, mac))

    async def unblock_client(self, site, mac):
        self.calls.append(("unblock", site, mac))

    async def reboot_device(self, site, mac):
        self.calls.append(("reboot", mac))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def fake_client():
    client = FakeClient()
    with patch("custom_components.open_omada.build_client", return_value=client):
        yield client


@pytest.fixture
def config_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id=OID, title="Omada")
    entry.add_to_hass(hass)
    return entry
