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
# DPI client traffic is a heavier query; refresh it less often (seconds).
TOP_WAN_INTERVAL = 300

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.SWITCH,
]
