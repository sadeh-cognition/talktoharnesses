"""Domain error type with stable error codes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from talktoharnesses.domain.enums import ErrorCode


class DomainError(Exception):
    """Raised when a pure transition or contract check fails."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details or {})
        super().__init__(message)

    def __repr__(self) -> str:
        return (
            f"DomainError(code={self.code!r}, message={self.message!r}, details={self.details!r})"
        )
