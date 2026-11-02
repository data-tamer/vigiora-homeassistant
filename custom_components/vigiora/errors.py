"""Map Vigiora API errors to Home Assistant errors with clear messages."""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .api import (
    VigioraApiError,
    VigioraApprovalRequired,
    VigioraAuthError,
    VigioraConnectionError,
    VigioraError,
)
from .const import DOMAIN


def to_ha_error(err: VigioraError) -> HomeAssistantError:
    """Translate an API error for an entity action or a service call."""
    if isinstance(err, VigioraApprovalRequired):
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="approval_required",
            translation_placeholders={"operation": err.operation_id},
        )
    if isinstance(err, VigioraAuthError):
        return HomeAssistantError(translation_domain=DOMAIN, translation_key="unauthenticated")
    if isinstance(err, VigioraConnectionError):
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        )
    if isinstance(err, VigioraApiError):
        placeholders = {"code": err.code, "message": err.message}
        if err.status == 402:
            key = "payment_required"
        elif err.status == 403 and err.code == "insufficient_scope":
            key = "insufficient_scope"
            placeholders["scope"] = str(err.details.get("required_scope") or "?")
        elif err.status == 403:
            key = "forbidden"
        elif err.status == 409:
            key = "conflict"
        elif err.status == 429:
            key = "rate_limited"
        elif err.status == 404:
            key = "not_found"
        else:
            key = "api_error"
            placeholders["status"] = str(err.status)
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders=placeholders,
        )
    return HomeAssistantError(str(err))
