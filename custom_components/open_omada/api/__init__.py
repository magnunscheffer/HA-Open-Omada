"""Home Assistant independent Omada Open API client."""

from .client import OmadaClient, format_mac
from .exceptions import (
    OmadaApiError,
    OmadaAuthError,
    OmadaConnectionError,
    OmadaError,
    OmadaNotSupportedError,
)

__all__ = [
    "OmadaApiError",
    "OmadaAuthError",
    "OmadaClient",
    "OmadaConnectionError",
    "OmadaError",
    "OmadaNotSupportedError",
    "format_mac",
]
