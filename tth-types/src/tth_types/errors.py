"""Domain error type with stable error codes."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final, cast

from tth_types.enums import ErrorCode

# Short generic messages for diagnostic/HTTP sinks. Never echo provider- or
# request-controlled DomainError.message text through these surfaces.
_PUBLIC_MESSAGES: Final[dict[ErrorCode, str]] = {
    ErrorCode.SANDBOX_POLICY_REQUIRED: "Select a project sandbox policy before starting a harness.",
    ErrorCode.SANDBOX_POLICY_DENIED: "The project sandbox policy does not permit this operation.",
    ErrorCode.CREDENTIAL_PROXY_UNSUPPORTED: (
        "This provider login cannot be proxied safely. Configure a supported host login; "
        "credentials will not be copied into the sandbox."
    ),
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
    ErrorCode.SANDBOX_PREPARING: "harness sandbox is being prepared; retry shortly",
    ErrorCode.SANDBOX_UNAVAILABLE: "harness sandbox unavailable",
    ErrorCode.SANDBOX_PATH_NOT_MOUNTED: (
        "path is not mounted into the harness sandbox; working directories and "
        "workspace roots must live under a mounted host root"
    ),
    ErrorCode.WORKSPACE_SETUP_FAILED: "workspace setup (.tth/setup.sh) failed",
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

# Fixed vocabulary for sandbox failures; details["reason"] values outside this
# table fall back to the generic sandbox_unavailable message.
_SANDBOX_UNAVAILABLE_REASONS: Final[dict[str, str]] = {
    "docker_unavailable": "Docker is not reachable on the TalkToHarnesses host",
    "image_build_failed": ("sandbox image build failed; check TalkToHarnesses server logs"),
    "build_context_missing": (
        "sandbox image is missing and no build context is available; "
        "build it with deploy/build-splits.sh"
    ),
    "auth_file_missing": (
        "provider credential file for this harness was not found on the TalkToHarnesses host"
    ),
    "health_timeout": "sandbox container did not become healthy",
    "port_conflict": "sandbox port is already in use on the TalkToHarnesses host",
    "container_start_failed": (
        "sandbox container failed to start; check TalkToHarnesses server logs"
    ),
}

# Fixed vocabulary for repo-declared workspace setup failures; the script's
# own output never reaches these messages.
_WORKSPACE_SETUP_REASONS: Final[dict[str, str]] = {
    "exit_status": "workspace setup script (.tth/setup.sh) exited with an error",
    "timeout": "workspace setup script (.tth/setup.sh) timed out",
    "lock_timeout": (
        "workspace setup is already running for this working directory; retry shortly"
    ),
    "runner_error": (
        "workspace setup could not be executed in the sandbox; check TalkToHarnesses server logs"
    ),
}


def public_message(code: ErrorCode, *, details: Mapping[str, Any] | None = None) -> str:
    """Return a short generic message for a stable error code."""
    if code is ErrorCode.PROVIDER_INCOMPATIBLE:
        if details is not None and details.get("reason") in (
            "authentication_required",
            "authentication_failed",
        ):
            return "harness authentication failed; refresh the provider credentials on the TTH host"
        version_message = _version_mismatch_message(details)
        if version_message is not None:
            return version_message
    if code is ErrorCode.SANDBOX_UNAVAILABLE:
        reason_message = _reason_message(_SANDBOX_UNAVAILABLE_REASONS, details)
        if reason_message is not None:
            return reason_message
    if code is ErrorCode.WORKSPACE_SETUP_FAILED:
        reason_message = _reason_message(_WORKSPACE_SETUP_REASONS, details)
        if reason_message is not None:
            return reason_message
    return _PUBLIC_MESSAGES.get(code, _DEFAULT_PUBLIC_MESSAGE)


def _reason_message(table: Mapping[str, str], details: Mapping[str, Any] | None) -> str | None:
    if details is None:
        return None
    reason = details.get("reason")
    if not isinstance(reason, str):
        return None
    return table.get(reason)


def _version_mismatch_message(details: Mapping[str, Any] | None) -> str | None:
    if details is None:
        return None
    provider = details.get("provider")
    installed = details.get("installed_version")
    supported = details.get("supported_versions")
    if not isinstance(provider, str) or not isinstance(installed, str):
        return None
    if not isinstance(supported, list):
        return None
    versions: list[str] = []
    for raw_item in cast(list[object], supported):
        if not isinstance(raw_item, str):
            return None
        if not _SAFE_VERSION_VALUE.fullmatch(raw_item):
            return None
        versions.append(raw_item)
    if not _SAFE_VERSION_VALUE.fullmatch(provider) or not _SAFE_VERSION_VALUE.fullmatch(installed):
        return None
    return (
        f"{provider} version {installed} is incompatible; supported versions: {', '.join(versions)}"
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
