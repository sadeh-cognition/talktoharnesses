"""Cursor ACP extension allowlists and validators.

Protocol-identical ACP v1 session/update and permission shapes are reused from
``schemas.base``. Cursor-only control notifications and future extension
variants live here.
"""

from __future__ import annotations

from typing import Any

from talktoharnesses.providers.acp.schemas.base import (
    is_allowlisted_permission_request,
    is_allowlisted_session_update,
)

# Captured Cursor control-plane notifications. Empty until fixtures prove them.
CURSOR_CONTROL_NOTIFICATIONS: frozenset[str] = frozenset()


def is_allowlisted_cursor_session_update(params: dict[str, Any] | None) -> bool:
    return is_allowlisted_session_update(params)


def is_allowlisted_cursor_permission_request(params: dict[str, Any] | None) -> bool:
    return is_allowlisted_permission_request(params)
