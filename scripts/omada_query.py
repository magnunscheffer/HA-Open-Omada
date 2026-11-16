"""Read-only CLI: prints the Omada controller as the entity tree HA will get.

Usage:
    .venv/bin/python scripts/omada_query.py            # tree view
    .venv/bin/python scripts/omada_query.py --json     # snapshot as JSON
    .venv/bin/python scripts/omada_query.py --site "My Site"

Configuration comes from .env (see .env.example).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import sys
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "open_omada"))

from api import OmadaClient, OmadaError  # noqa: E402
from api.snapshot import fetch_snapshot  # noqa: E402


def _onoff(v: bool | None) -> str:
    return "-" if v is None else ("ON" if v else "OFF")


def _pct(v) -> str:
    return "-" if v is None else f"{v}%"


def print_tree(snap) -> None:
    print(f"Omada Controller {snap.version}  (omadacId {snap.omadac_id})")
    for s in snap.sites:
        print(f"└─ Site: {s.site.name}  [{len(s.devices)} devices]")

        print("   ├─ Firewalls")
        for g in s.gateways:
            d = g.device
            print(f"   │  └─ {d.name}  {d.model}  fw {d.firmware}  {d.status}  "
                  f"CPU {_pct(g.cpu)}  MEM {_pct(g.mem)}  [button: reboot]")
            for w in g.wans:
                pub = "public" if w.ip_is_public else "private (public IP not exposed by API)"
                role = f" [{w.role}{', IN USE' if w.in_use else ''}]" if w.role else ""
                print(f"   │     ├─ WAN {w.name}{role}: link {_onoff(w.connected)} internet {_onoff(w.internet)}"
                      f"  {w.proto}  IP {w.ip} [{pub}]  ↓{w.rx_rate_kbps} ↑{w.tx_rate_kbps} kbit/s"
                      f"  lat {w.latency_ms}ms loss {w.loss_pct}%")
        if s.acls:
            print("   │  ├─ ACLs (switch)")
            for r in s.acls:
                print(f"   │  │  └─ #{r.index} {r.name}: {_onoff(r.enabled)}")
        if s.port_forwards:
            print("   │  ├─ Port forwarding (switch)")
            for r in s.port_forwards:
                print(f"   │  │  └─ {r.name}: {_onoff(r.enabled)}  {r.raw.get('externalPort')} -> "
                      f"{r.raw.get('forwardIp')}:{r.raw.get('forwardPort')}")
        if s.policy_routes:
            print("   │  └─ Policy Routes (switch)")
            for r in s.policy_routes:
                print(f"   │     └─ #{r.index} {r.name}: {_onoff(r.enabled)}")

        print("   ├─ Switches")
        for sw in s.switches:
            d = sw.device
            print(f"   │  └─ {d.name}  {d.model}  fw {d.firmware}  {d.status}  CPU {_pct(sw.cpu)}"
                  f"  MEM {_pct(sw.mem)}  PoE {'yes' if sw.supports_poe else 'no'}  [button: reboot]")
            for p in sw.ports:
                link = "UP" if p.link_up else ("down" if p.link_up is False else "?")
                speed = f"{p.speed_mbps}M" if p.speed_mbps else ""
                poe = "" if p.poe_enabled is None else (
                    f"  PoE {_onoff(p.poe_enabled)}{' (powering)' if p.poe_active else ''} (switch)")
                lag = "  [LAG]" if p.lag_member else ""
                print(f"   │     ├─ {p.port:>2} {p.name:<14} admin {_onoff(p.enabled)} (switch)"
                      f"  link {link} {speed}{poe}{lag}")

        print("   └─ Access Points")
        for ap in s.access_points:
            d = ap.device
            print(f"      └─ {d.name}  {d.model}  fw {d.firmware}  {d.status}  CPU {_pct(ap.cpu)}"
                  f"  MEM {_pct(ap.mem)}  [button: reboot]")
            for ss in ap.ssids:
                vlan = f" VLAN {ss.vlan_id}" if ss.vlan_id else ""
                print(f"         ├─ SSID {ss.name}: {_onoff(ss.enabled)} (switch)  "
                      f"{'/'.join(ss.bands)}{vlan}{' guest' if ss.guest else ''}"
                      f"{'' if ss.broadcast else ' hidden'}")

        for e in s.errors:
            print(f"   ! {e}")


def _to_jsonable(obj):
    if dataclasses.is_dataclass(obj):
        return {f.name: _to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)
                if f.name != "raw"}
    if isinstance(obj, list):
        return [_to_jsonable(x) for x in obj]
    return obj


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="print snapshot as JSON")
    ap.add_argument("--site", help="site name or id to limit the query")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)

    load_dotenv(ROOT / ".env")
    try:
        url, cid, secret = (os.environ[k] for k in ("OMADA_URL", "OMADA_CLIENT_ID", "OMADA_CLIENT_SECRET"))
    except KeyError as err:
        print(f"Missing {err} in .env (copy .env.example)", file=sys.stderr)
        return 2
    verify = os.getenv("OMADA_VERIFY_SSL", "true").lower() == "true"

    async with aiohttp.ClientSession() as session:
        client = OmadaClient(session, url, cid, secret, verify_ssl=verify)
        try:
            await client.get_controller_info()
            site_ids = None
            if args.site:
                site_ids = [s["siteId"] for s in await client.get_sites()
                            if args.site in (s["siteId"], s["name"])]
                if not site_ids:
                    print(f"Site {args.site!r} not found", file=sys.stderr)
                    return 1
            snap = await fetch_snapshot(client, site_ids)
        except OmadaError as err:
            print(f"ERROR: {err}", file=sys.stderr)
            return 1

    if args.json:
        print(json.dumps(_to_jsonable(snap), indent=2, ensure_ascii=False))
    else:
        print_tree(snap)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
