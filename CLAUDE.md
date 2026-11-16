# CLAUDE.md — HA-Open-Omada

Home Assistant custom integration (domain `open_omada`) for TP-Link Omada SDN via the
official **Open API**. Target distribution: HACS. Repo: github.com/magnunscheffer/HA-Open-Omada.

## Current phase
v0.1 done: API layer + CLI. v0.2 in progress: config flow (user/reauth/reconfigure),
coordinator, platforms sensor/binary_sensor/switch/button, diagnostics, en/pt-BR.
Device tree: Omada Controller (service device, holds SSID switches; each SSID lists its
APs in `access_points`) → gateway/switch/AP via `via_device_id` (registered in setup).
ACL/policy-route switches live on the site's gateway device.
Next: validate in dev HA (`.venv-ha`, config `ha-config/`), then CI (hassfest, HACS action,
pytest), MIT LICENSE, `brand/icon.png` (own art, no TP-Link logo; needs HA ≥ 2026.3),
make repo public, GitHub release.

## Rules
- `custom_components/open_omada/api/` must stay free of `homeassistant` imports.
- Read `docs/api-reference.md` and the published spec before calling new endpoints.
  Spec source: `https://use1-omada-northbound.tplinkcloud.com/v3/api-docs/<group>`,
  e.g. `03%20Device`. **Never** pull `/v3/api-docs` from the local controller; it
  hung the controller during development.
- Be gentle with the controller: client semaphore (default 2), sequential snapshot,
  default scan interval 60s.
- Secrets live in `.env` (gitignored). Never commit them; tests use synthetic data.
- The API app now has ADMIN rights. NEVER trigger a write (switch, PoE, ACL, policy route,
  SSID, reboot) without the user's explicit approval for that specific action. All write
  paths were validated on the real controller on 2026-11-16 (see docs/api-reference.md).
- Write validation status lives in `docs/api-reference.md` ("Escritas validadas"). Test only
  on targets the user approves, restore the original state, stop at the first error.
- ACL/policy route have no status-only endpoint: PUT the GET object filtered to
  `ACL_PUT_FIELDS` / `POLICY_ROUTE_PUT_FIELDS` with `status` changed.

## Lab
Maintainer lab: controller 6.3.0.45 (self-signed cert → `OMADA_VERIFY_SSL=false`), one site with an
ER605 gateway, SG2218P (smart) + 2× ES206GP (Agile, `/switches/es/`), 2× EAP670. URL and credentials
only in the local `.env`.

## Commands
```
.venv/bin/python scripts/omada_query.py [--json] [--site NAME]
.venv/bin/pytest -q && .venv/bin/ruff check .               # API tests
.venv-test/bin/python -m pytest tests/integration -q -p no:cacheprovider  # HA tests (own venv)
.venv-ha/bin/hass -c ha-config     # dev HA pinned to 2026.10.4 (= user's prod) on :8123
```
Keep `.venv-ha` and `.venv-test` separate: the test framework pins its own HA version and
once downgraded the dev HA, breaking its `.storage`.
```
```
