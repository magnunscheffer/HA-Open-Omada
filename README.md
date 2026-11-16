# Open Omada for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Validate](https://github.com/magnunscheffer/HA-Open-Omada/actions/workflows/validate.yml/badge.svg)](https://github.com/magnunscheffer/HA-Open-Omada/actions/workflows/validate.yml)
[![Tests](https://github.com/magnunscheffer/HA-Open-Omada/actions/workflows/tests.yml/badge.svg)](https://github.com/magnunscheffer/HA-Open-Omada/actions/workflows/tests.yml)

Community integration for **TP-Link Omada SDN** that uses the controller's **official Open API**
(`/openapi/…`, client credentials) instead of the internal web API.

> **Beta.** Not affiliated with or endorsed by TP-Link. Omada is a trademark of TP-Link.
> Leia em português: [README.pt-BR.md](README.pt-BR.md)

## Why another Omada integration?

| | Open Omada | Core `tplink_omada` |
|---|---|---|
| API | Official Open API, client ID/secret | Internal web API, username/password (no 2FA) |
| Gateway ACLs, policy routes, port forwarding on/off | ✅ | — |
| SSIDs on/off | ✅ | — |
| Switch port on/off (same as the Omada UI) | ✅ | — |
| PoE on/off and live PoE power | ✅ | ✅ |
| Active WAN (failover), WAN throughput/latency/loss | ✅ | link/online only |
| Top WAN clients (gateway DPI) | ✅ | — |
| Client reconnect / block / unblock | ✅ | reconnect only |
| Reboot any device | ✅ | — |
| Wi-Fi presence (device tracker), firmware updates | — | ✅ |

## Requirements

- Omada Software Controller / OC200 / OC300 with Open API (tested on **6.3.0.45**)
- Home Assistant **2026.3** or newer

## Create the Open API application in Omada

1. Omada controller → **Global View → Settings → Platform Integration → Open API → Add New App**.
2. **Mode: Client**. Role: a *Viewer* role is enough for monitoring; switches, buttons and
   actions need a role that can modify the site (e.g. *Administrator*).
3. Copy the **Client ID** and **Client Secret**.

## Installation

**HACS (custom repository)**

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/magnunscheffer/HA-Open-Omada`,
   category **Integration**. Enable *Show beta versions* to see pre-releases.
2. Install **Open Omada** and restart Home Assistant.

**Manual:** copy `custom_components/open_omada` into your `config/custom_components/` and restart.

## Configuration

**Settings → Devices & services → Add integration → Open Omada**

| Field | Description |
|---|---|
| URL | Controller address, e.g. `https://omada.local:8043` |
| Client ID / Client Secret | From the Open API application |
| Verify SSL certificate | Turn off for self-signed controller certificates |

Re-authentication and reconfiguration are supported from the integration page.

## Devices and entities

```
Omada Controller (service)   devices count · one switch per SSID (attribute: access_points)
├─ Gateway                    online · CPU · memory · reboot · active WAN · top WAN client
│   ├─ per WAN               link · internet · in use · IP · download · upload · latency · loss
│   └─ switches              ACLs · policy routes · port forwarding
├─ Switch                     online · CPU · memory · reboot
│   └─ per port              enabled · link · PoE on/off · PoE power · PoE powering · speed*
└─ Access point               online · CPU · memory · reboot
```
\* disabled by default (also: uptime).

### Actions

| Action | Fields |
|---|---|
| `open_omada.reconnect_client` | `mac` |
| `open_omada.block_client` | `mac` (Omada blocklist) |
| `open_omada.unblock_client` | `mac` |

`config_entry_id` is only needed with more than one controller.

### How switches behave

- **Port on/off** works like the Omada UI: *off* applies the built-in **Disable** profile; *on*
  restores the profile the port had (remembered), or **All** if unknown.
- **PoE on/off** (controller ≥ 6.2.10 only allows it with profile override): *off* enables
  override on the port and turns PoE off; *on* turns PoE on and returns the port to its profile.
  Ports with **custom VLAN tagging** (trunks) are refused, because enabling override would need
  the VLAN lists, which the Open API does not return. Enable *Profile Override* on those ports in
  the Omada UI and the integration will only toggle PoE.
- ACLs, policy routes and port forwarding are updated by sending the full rule back with only
  `status` changed (the API has no status-only endpoint).

## Data updates

One coordinated poll every **60 s** (requests are serialised to protect the controller). The DPI
"top WAN client" query refreshes every 5 minutes over a 1-hour window.

## Known limitations

- **Public IP per WAN** is not exposed by the Open API. When a WAN gets a private address (modem,
  CGNAT) only that address is available.
- **Top WAN clients** need **DPI** enabled on the gateway (Omada → Network Security → Application
  Control). Per-client traffic without DPI includes LAN traffic, so it is not used.
- PoE "powering" status depends on what the controller reports for each switch model.
- Do not download the API docs from the controller itself (`/v3/api-docs`): generating them can
  hang the controller. Use TP-Link's published copy.

## Troubleshooting

- Download **diagnostics** from the integration page (secrets, MACs and IPs are redacted).
- Debug logging:
  ```yaml
  logger:
    logs:
      custom_components.open_omada: debug
  ```

## Removal

Remove the integration from **Settings → Devices & services**, then (optionally) delete the Open
API application in Omada. Uninstall from HACS.

## Development

```bash
python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q && .venv/bin/ruff check .              # API layer
python3.13 -m venv .venv-test && .venv-test/bin/pip install pytest-homeassistant-custom-component
.venv-test/bin/python -m pytest tests/integration -q        # HA integration
cp .env.example .env && .venv/bin/python scripts/omada_query.py   # live read-only dump
```

The `custom_components/open_omada/api/` package has no Home Assistant dependency. Endpoint notes
and the list of write paths validated on real hardware: [docs/api-reference.md](docs/api-reference.md).

## License

[MIT](LICENSE)
