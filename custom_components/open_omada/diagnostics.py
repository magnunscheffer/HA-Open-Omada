"""Diagnostics with secrets and network identifiers redacted."""

from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.core import HomeAssistant

from .api.redact import REDACT_KEYS, redact
from .coordinator import OmadaConfigEntry

# Device/host names and the controller URL (FQDN) are kept on purpose (owner decision).
TO_REDACT = REDACT_KEYS


def _plain(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj):
        return {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, list):
        return [_plain(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    return obj


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: OmadaConfigEntry
) -> dict[str, Any]:
    snap = entry.runtime_data.data
    return {
        "entry": redact(dict(entry.data)),
        "snapshot": redact(_plain(snap)),
    }
