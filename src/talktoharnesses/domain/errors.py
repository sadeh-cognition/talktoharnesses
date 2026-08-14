"""Domain error type with stable error codes."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from talktoharnesses.domain.enums import ErrorCode

# Short generic messages for diagnostic/HTTP sinks. Never echo provider- or
# request-controlled DomainError.message text through these surfaces.
_PUBLIC_MESSAGES: Final[dict[ErrorCode, str]] = {
    ErrorCode.PERSISTENCE_REQUIRED: "persistence required",
    ErrorCode.CONVERSATION_BUSY: "conversation busy",
    ErrorCode.MODE_CHANGE_WHILE_ACTIVE: "mode change while active",
    ErrorCode.UNSUPPORTED_NATIVE_EVENT: "unsupported native event",
    ErrorCode.PROTOCOL_ERROR: "protocol error",
    ErrorCode.PROVIDER_INCOMPATIBLE: "provider incompatible",
    ErrorCode.WORKING_DIRECTORY_NOT_FOUND: "working directory not found",
    ErrorCode.WORKSPACE_ROOT_NOT_FOUND: "workspace root not found",
    ErrorCode.INVALID_EXECUTABLE: "invalid executable",
    ErrorCode.EXECUTABLE_OWNER_MISMATCH: "executable owner mismatch",
    ErrorCode.RUNTIME_TIMEOUT: "runtime timeout",
    ErrorCode.INVALID_STATE: "invalid state",
    ErrorCode.INTERACTION_ALREADY_RESOLVED: "interaction already resolved",
    ErrorCode.QUEUED_PROMPT_NOT_EDITABLE: "queued prompt not editable",
    ErrorCode.UNKNOWN_HARNESS_KIND: "unknown harness kind",
    ErrorCode.DUPLICATE_REGISTRATION: "duplicate registration",
    ErrorCode.HARNESS_NOT_REGISTERED: "harness not registered",
    ErrorCode.NO_ACTIVE_TURN: "no active turn",
    ErrorCode.NO_QUEUED_PROMPT: "no queued prompt",
    ErrorCode.IDEMPOTENCY_CONFLICT: "idempotency conflict",
    ErrorCode.HARNESS_IN_USE: "harness in use",
    ErrorCode.OPTIMISTIC_CONFLICT: "optimistic conflict",
    ErrorCode.STALE_OWNER: "stale owner",
    ErrorCode.WORKER_LEASE_UNAVAILABLE: "worker lease unavailable",
    ErrorCode.INVALID_CURSOR: "invalid cursor",
    ErrorCode.INVALID_SEARCH_QUERY: "invalid search query",
    ErrorCode.NOT_FOUND: "not found",
}

_DEFAULT_PUBLIC_MESSAGE: Final = "conflict"
_SAFE_VERSION_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .()_\[\]\-]{0,127}$")


def public_message(
    code: ErrorCode, *, details: Mapping[str, Any] | None = None
) -> str:
    """Return a short generic message for a stable error code."""
    if code is ErrorCode.PROVIDER_INCOMPATIBLE:
        version_message = _version_mismatch_message(details)
        if version_message is not None:
            return version_message
    return _PUBLIC_MESSAGES.get(code, _DEFAULT_PUBLIC_MESSAGE)


def _version_mismatch_message(details: Mapping[str, Any] | None) -> str | None:
    if details is None:
        return None
    provider = details.get("provider")
    installed = details.get("installed_version")
    supported = details.get("supported_versions")
    if (
        not isinstance(provider, str)
        or not isinstance(installed, str)
        or not isinstance(supported, list)
        or not all(isinstance(version, str) for version in supported)
        or not _SAFE_VERSION_VALUE.fullmatch(provider)
        or not _SAFE_VERSION_VALUE.fullmatch(installed)
        or not all(_SAFE_VERSION_VALUE.fullmatch(version) for version in supported)
    ):
        return None
    return (
        f"{provider} version {installed} is incompatible; supported versions: "
        f"{', '.join(supported)}"
    )


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
