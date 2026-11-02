"""Errors of the vigiora_things client."""

from __future__ import annotations

from typing import Any


class ThingsError(Exception):
    """Base error."""


class ThingsConnectionError(ThingsError):
    """The server could not be reached (network, DNS, TLS, timeout)."""


class ThingsApiError(ThingsError):
    """The API answered with an error document."""

    def __init__(
        self, status: int, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        """Initialise from the error document."""
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


class ThingsAuthError(ThingsApiError):
    """401: the key is missing, wrong, expired or revoked."""


class ThingsNotSupported(ThingsError):
    """The server is older than the Things model (no /api/v1/things)."""
