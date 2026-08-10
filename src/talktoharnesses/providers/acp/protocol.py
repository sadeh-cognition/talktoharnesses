"""Adapter-owned ACP allowlists and decoders for AcpConnection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from talktoharnesses.providers.acp.schemas.base import (
    ALLOWED_INBOUND_METHODS,
    ALLOWED_OUTBOUND_METHODS,
    GROK_CONTROL_NOTIFICATIONS,
    is_allowlisted_permission_request,
    is_allowlisted_session_update,
)
from talktoharnesses.providers.acp.schemas.cursor_ext import CURSOR_EXTRA_OUTBOUND_METHODS

# Cursor may call session/set_config_option; Grok must not gain that method.
CURSOR_ALLOWED_OUTBOUND_METHODS: frozenset[str] = (
    ALLOWED_OUTBOUND_METHODS | CURSOR_EXTRA_OUTBOUND_METHODS
)

ParamsValidator = Callable[[dict[str, Any] | None], bool]


@dataclass(frozen=True, slots=True)
class AcpProtocolConfig:
    """Strict per-adapter ACP surface. Not a plugin registration API."""

    outbound_methods: frozenset[str]
    inbound_request_methods: frozenset[str]
    control_notifications: frozenset[str]
    session_update_validator: ParamsValidator
    permission_request_validator: ParamsValidator
    control_notification_prefix: str | None = None

    def is_outbound_method(self, method: str) -> bool:
        return method in self.outbound_methods

    def is_inbound_request_method(self, method: str) -> bool:
        return method in self.inbound_request_methods

    def is_control_notification(self, method: str) -> bool:
        if method in self.control_notifications:
            return True
        prefix = self.control_notification_prefix
        return bool(prefix and method.startswith(prefix))

    def is_inbound_method(self, method: str) -> bool:
        if method in self.inbound_request_methods:
            return True
        if method == "session/update":
            return True
        return self.is_control_notification(method)


def grok_acp_protocol() -> AcpProtocolConfig:
    """Grok-pinned ACP allowlist (ACP v1 + grok_ext control notifications)."""
    return AcpProtocolConfig(
        outbound_methods=ALLOWED_OUTBOUND_METHODS,
        inbound_request_methods=ALLOWED_INBOUND_METHODS,
        control_notifications=GROK_CONTROL_NOTIFICATIONS,
        session_update_validator=is_allowlisted_session_update,
        permission_request_validator=is_allowlisted_permission_request,
        # Grok emits additive `_x.ai/*` control notifications across CLI builds.
        control_notification_prefix="_x.ai/",
    )


def cursor_acp_protocol() -> AcpProtocolConfig:
    """Cursor-pinned ACP allowlist (ACP v1 + cursor_ext control notifications)."""
    from talktoharnesses.providers.acp.schemas.cursor_ext import (
        CURSOR_CONTROL_NOTIFICATIONS,
        is_allowlisted_cursor_permission_request,
        is_allowlisted_cursor_session_update,
    )

    return AcpProtocolConfig(
        outbound_methods=CURSOR_ALLOWED_OUTBOUND_METHODS,
        inbound_request_methods=ALLOWED_INBOUND_METHODS,
        control_notifications=CURSOR_CONTROL_NOTIFICATIONS,
        session_update_validator=is_allowlisted_cursor_session_update,
        permission_request_validator=is_allowlisted_cursor_permission_request,
    )
