from api.models import Device, Rule, Ssid, Switch, Wan, apply_wan_roles

DEV = {"mac": "AA-BB-CC-00-00-01", "name": "sw", "type": "switch", "subtype": "es",
       "model": "ES206GP v1.20", "status": 1, "firmwareVersion": "6.3.0", "cpuUtil": 3, "memUtil": 60}


def test_device_status_and_agile():
    d = Device.from_api(DEV)
    assert d.online and d.status == "connected"
    assert d.is_agile_switch


def test_wan_rate_conversion_and_public_flag():
    pub = Wan.from_api({"port": 1, "name": "WAN1", "status": 1, "internetState": 1,
                        "ip": "8.8.4.4", "rxRate": 100, "txRate": 10})
    assert pub.rx_rate_kbps == 800.0 and pub.tx_rate_kbps == 80.0
    assert pub.ip_is_public
    for ip in ("192.168.100.10", "100.64.1.1", None):
        assert not Wan.from_api({"port": 2, "ip": ip}).ip_is_public


def test_switch_ports_merge_info_and_stat():
    info = {"cpuUtil": 5, "memUtil": 50, "portList": [
        {"port": 1, "name": "P1", "status": 1, "poeMode": 1},
        {"port": 2, "name": "P2", "status": 0, "poeMode": 0},
        {"port": 3, "name": "SFP", "status": 1, "poeMode": 1},
    ]}
    stat = {"supportPoe": True, "ports": [
        {"port": 1, "type": 1, "portStatus": {"linkStatus": 1, "linkSpeed": 3, "poe": True}},
        {"port": 2, "type": 1, "portStatus": {"linkStatus": 0, "linkSpeed": 1, "poe": False}},
        {"port": 3, "type": 3, "portStatus": {"linkStatus": 0, "linkSpeed": 1}},
    ]}
    sw = Switch.from_api(Device.from_api(DEV), info, stat)
    p1, p2, p3 = sw.ports
    assert p1.enabled and p1.link_up and p1.speed_mbps == 1000 and p1.poe_enabled and p1.poe_active
    assert not p2.enabled and p2.link_up is False and p2.speed_mbps is None and p2.poe_enabled is False
    assert p3.poe_enabled is None  # SFP never PoE


def test_switch_without_poe():
    info = {"portList": [{"port": 1, "status": 1, "poeMode": 0}]}
    sw = Switch.from_api(Device.from_api(DEV), info, {"supportPoe": False, "ports": []})
    assert sw.ports[0].poe_enabled is None and sw.ports[0].link_up is None


def test_ssid_bands_and_rules():
    s = Ssid.from_api({"id": "x", "name": "Home", "ssidEnable": False, "band": 3,
                       "vlanEnable": True, "vlanId": 10}, "w1")
    assert s.bands == ["2.4G", "5G"] and not s.enabled and s.vlan_id == 10
    assert Rule.acl({"id": "a", "index": 1, "description": "Deny", "status": True}).enabled
    assert not Rule.policy_route({"id": "r", "name": "VIVO", "status": False}).enabled


def test_poe_info_is_authoritative_for_capability_and_power():
    info = {"portList": [{"port": n, "status": 1, "poeMode": 1} for n in range(1, 7)]}
    stat = {"supportPoe": True, "ports": [{"port": n, "type": 1, "portStatus": {"linkStatus": 1}} for n in range(1, 7)]}
    poe = {n: {"port": n, "supportPoe": n <= 4, "power": 2.7 if n == 1 else (0.0 if n <= 4 else None)}
           for n in range(1, 7)}
    sw = Switch.from_api(Device.from_api(DEV), info, stat, poe)
    assert [p.port for p in sw.ports if p.poe_enabled is not None] == [1, 2, 3, 4]
    assert sw.ports[0].poe_power_w == 2.7 and sw.ports[0].poe_active
    assert sw.ports[1].poe_power_w == 0.0 and not sw.ports[1].poe_active
    assert sw.ports[4].poe_power_w is None
    # Without poe-info: fall back to every copper port, no power reading.
    fallback = Switch.from_api(Device.from_api(DEV), info, stat)
    assert all(p.poe_enabled is not None and p.poe_power_w is None for p in fallback.ports)


def _wans(w1_internet=True, w2_internet=True):
    return [Wan.from_api({"port": 1, "name": "WAN1", "internetState": int(w1_internet)}),
            Wan.from_api({"port": 2, "name": "WAN/LAN2", "internetState": int(w2_internet)})]


def test_failover_primary_up_backup_standby():
    lb = {"linkBackup": True, "primaryWans": ["2_abc"], "backupWan": "1_def", "mode": 0}
    w1, w2 = _wans()
    assert apply_wan_roles([w1, w2], lb) == "failover"
    assert (w2.role, w2.in_use) == ("primary", True)
    assert (w1.role, w1.in_use) == ("backup", False)


def test_failover_primary_down_backup_takes_over():
    lb = {"linkBackup": True, "primaryWans": ["2_abc"], "backupWan": "1_def", "mode": 0}
    w1, w2 = _wans(w2_internet=False)
    apply_wan_roles([w1, w2], lb)
    assert w2.in_use is False and w1.in_use is True


def test_failover_scheduled_mode_is_unknown_and_load_balance():
    w1, w2 = _wans()
    apply_wan_roles([w1, w2], {"linkBackup": True, "primaryWans": ["2_a"], "backupWan": "1_b", "mode": 2})
    assert w1.in_use is None
    w1, w2 = _wans(w1_internet=False)
    assert apply_wan_roles([w1, w2], {"linkBackup": False}) == "load_balance"
    assert (w1.in_use, w2.in_use) == (False, True)


async def test_top_wan_clients_ranked_and_dpi_off():
    from api.snapshot import fetch_top_wan_clients

    class C:
        dpi = True
        async def get_dpi_enabled(self, site):
            return self.dpi
        async def get_client_wan_traffic(self, site, start, end):
            assert end - start == 3600
            return [
                {"mac": "a", "clientName": "TV", "download": 900, "upload": 100,
                 "applicationList": [{"applicationName": "YouTube", "traffic": 5},
                                     {"applicationName": "Netflix", "traffic": 9}]},
                {"mac": "b", "clientName": "Laptop", "download": 5000, "upload": 1000},
                {"mac": "c", "clientName": "Idle", "download": 0, "upload": 0},
            ]

    c = C()
    top = await fetch_top_wan_clients(c, "s", now=10_000)
    assert [x.name for x in top.clients] == ["Laptop", "TV"]  # idle client dropped
    c.dpi = False
    off = await fetch_top_wan_clients(c, "s")
    assert not off.dpi_enabled and off.clients == []


async def test_top_wan_lookback_uses_latest_window_with_data():
    from api.snapshot import fetch_top_wan_clients

    class C:
        def __init__(self, newest):
            self.newest = newest  # DPI data exists only for windows ending at or before this
            self.calls = []

        async def get_dpi_enabled(self, site):
            return True

        async def get_client_wan_traffic(self, site, start, end):
            self.calls.append(end)
            if end > self.newest:
                return []
            return [{"mac": "a", "clientName": "RDS", "download": 900, "upload": 100}]

    now = 100_000
    fresh = await fetch_top_wan_clients(C(newest=now), "s", window_s=600, now=now, lookback_s=3600)
    assert fresh.clients and fresh.data_until == now and not fresh.delayed

    c = C(newest=now - 35 * 60)  # DPI batch 35 minutes late
    late = await fetch_top_wan_clients(c, "s", window_s=600, now=now, lookback_s=3600)
    assert late.delayed and late.data_until == now - 35 * 60 and late.clients[0].name == "RDS"
    assert len(c.calls) == 8  # now, -5, ..., -35 min

    c = C(newest=now - 2 * 3600)  # nothing in the last hour
    none = await fetch_top_wan_clients(c, "s", window_s=600, now=now, lookback_s=3600)
    assert none.clients == [] and none.data_until is None and none.delayed
    assert len(c.calls) == 13  # bounded: now .. -60 min

    no_lookback = await fetch_top_wan_clients(C(newest=now - 600), "s", window_s=86400, now=now)
    assert no_lookback.clients == [] and no_lookback.data_until == now and not no_lookback.delayed
