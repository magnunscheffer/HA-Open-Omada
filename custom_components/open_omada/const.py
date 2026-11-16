"""Constants for the Open Omada integration."""

import logging
from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "open_omada"
LOGGER = logging.getLogger(__package__)
MANUFACTURER = "TP-Link"

CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"

# The controller is easy to overload; one coordinated poll per minute.
SCAN_INTERVAL = timedelta(seconds=60)
# DPI "top WAN clients" windows: key -> (window s, refresh s, cold-start look-back s).
# The gateway DPI aggregates in ~5-minute buckets (a 2-minute window returns nothing) and the
# batches can reach the controller ~30+ minutes late. When a refresh comes back empty, the last
# result with data is kept (marked delayed, for up to TOP_WAN_KEEP); only on a cold start, with
# nothing to keep, the 10-minute window looks back up to 30 min for data.
TOP_WAN_WINDOWS: dict[str, tuple[int, int, int]] = {
    "10m": (600, 60, 1800),
    "24h": (86400, 60, 0),
}
TOP_WAN_KEEP = 1800  # older than 30 min: show "no recent data" instead

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.SWITCH,
]

# Bundled dashboard card (served by the integration, loaded in every dashboard).
CARD_FILE = "open-omada-top-wan-card.js"
CARD_URL = f"/{DOMAIN}/{CARD_FILE}"
