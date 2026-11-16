"""Exceptions raised by the Omada Open API client."""

from __future__ import annotations


class OmadaError(Exception):
    """Base error for the Omada Open API client."""


class OmadaConnectionError(OmadaError):
    """The controller could not be reached (network, TLS, timeout)."""


class OmadaAuthError(OmadaError):
    """Credentials were rejected or the token could not be obtained/refreshed."""


class OmadaApiError(OmadaError):
    """The controller answered with a non-zero ``errorCode``."""

    def __init__(self, error_code: int, msg: str, path: str = "") -> None:
        self.error_code = error_code
        self.msg = msg
        self.path = path
        super().__init__(f"Omada API error {error_code} on {path}: {msg}")


class OmadaNotSupportedError(OmadaApiError):
    """The device/controller does not support the requested feature."""
