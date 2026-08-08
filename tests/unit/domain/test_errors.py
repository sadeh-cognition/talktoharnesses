"""Domain error code stability."""

from __future__ import annotations

from talktoharnesses.domain import DomainError, ErrorCode


def test_required_error_codes_exist() -> None:
    required = {
        "persistence_required",
        "conversation_busy",
        "mode_change_while_active",
        "unsupported_native_event",
        "protocol_error",
        "provider_incompatible",
        "working_directory_not_found",
        "workspace_root_not_found",
        "invalid_executable",
        "executable_owner_mismatch",
        "runtime_timeout",
    }
    values = {c.value for c in ErrorCode}
    assert required <= values


def test_domain_error_carries_code_and_details() -> None:
    err = DomainError(
        ErrorCode.CONVERSATION_BUSY,
        "busy",
        details={"conversation_id": "x"},
    )
    assert err.code is ErrorCode.CONVERSATION_BUSY
    assert err.message == "busy"
    assert err.details == {"conversation_id": "x"}
    assert "CONVERSATION_BUSY" in repr(err) or "conversation_busy" in repr(err)
