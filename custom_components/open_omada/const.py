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
# DPI "top WAN clients" windows: key -> (window s, refresh s, look-back s).
# The gateway DPI aggregates in ~5-minute buckets (a 2-minute window returns nothing) and the
# batches can reach the controller ~30+ minutes late; the 10-minute view therefore falls back
# to the most recent 10 minutes that have data (up to 1 h back). 24 h always ends now.
TOP_WAN_WINDOWS: dict[str, tuple[int, int, int]] = {
    "10m": (600, 60, 3600),
    "24h": (86400, 300, 0),
}

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.SWITCH,
]

# Bundled dashboard card (served by the integration, loaded in every dashboard).
CARD_FILE = "open-omada-top-wan-card.js"
CARD_URL = f"/{DOMAIN}/{CARD_FILE}"
