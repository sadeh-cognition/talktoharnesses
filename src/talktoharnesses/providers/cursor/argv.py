"""Cursor process argument construction (no shell)."""

from __future__ import annotations

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError


def build_cursor_argv(
    *,
    model: str | None = None,
    mode: str | None = None,
) -> tuple[str, ...]:
    """Build argv after the resolved executable.

    The pinned ACP surface does not expose model or mode selection.
    """
    if model:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "cursor model selection is not supported by the pinned ACP release",
            details={"model": model},
        )
    if mode:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "cursor mode selection is not supported by the pinned ACP release",
            details={"mode": mode},
        )
    return ("acp",)
