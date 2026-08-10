"""Strict Pydantic model validation."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from talktoharnesses.domain import (
    CanonicalToolResult,
    Command,
    CommandKind,
    Conversation,
    ConversationStatus,
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessKind,
)
from talktoharnesses.domain.models import SubmitTurnPayload


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        HarnessConfiguration(
            kind=HarnessKind.GROK,
            working_directory="/tmp",
            unknown_field=True,  # type: ignore[call-arg]
        )


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValidationError):
        Conversation(
            owner_id="u1",
            status=ConversationStatus.IDLE,
            created_at=datetime(2026, 1, 1),  # naive
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_utc_datetime_accepted() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    conv = Conversation(
        owner_id="u1",
        created_at=now,
        updated_at=now,
    )
    assert conv.display_title == "Untitled conversation"
    assert conv.next_event_sequence == 1


def test_display_title_prefers_native_manual_then_derived() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    native = Conversation(
        owner_id="u1",
        title_native="Native title",
        title_manual="Manual title",
        title_derived="Derived title",
        created_at=now,
        updated_at=now,
    )
    assert native.display_title == "Native title"
    manual = Conversation(
        owner_id="u1",
        title_manual="Manual title",
        title_derived="Derived title",
        created_at=now,
        updated_at=now,
    )
    assert manual.display_title == "Manual title"
    derived = Conversation(
        owner_id="u1",
        title_derived="Derived title",
        created_at=now,
        updated_at=now,
    )
    assert derived.display_title == "Derived title"


def test_lazy_module_getattr_unknown_names() -> None:
    import talktoharnesses.application as application
    import talktoharnesses.django as django_pkg

    with pytest.raises(AttributeError):
        _ = application.not_a_real_export  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        _ = django_pkg.not_a_real_export  # type: ignore[attr-defined]


def test_tool_output_tail_byte_limit() -> None:
    big = "discarded" + "ä" * 2000 + "latest result"
    result = CanonicalToolResult(
        turn_id=uuid4(),
        tool_name="bash",
        output_tail=big,
    )
    assert len(result.output_tail.encode("utf-8")) <= 2048
    assert result.output_tail.endswith("latest result")
    assert not result.output_tail.startswith("discarded")


def test_capabilities_forbid_extra() -> None:
    with pytest.raises(ValidationError):
        HarnessCapabilities(
            kind=HarnessKind.CODEX,
            version="1",
            extra=1,  # type: ignore[call-arg]
        )


def test_models_reject_coercion() -> None:
    with pytest.raises(ValidationError):
        HarnessCapabilities(
            kind=HarnessKind.CODEX,
            version="1",
            supports_resume=1,  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        HarnessCapabilities(
            kind=HarnessKind.CODEX,
            version="1",
            models=[],  # type: ignore[arg-type]
        )


def test_command_kind_must_match_payload() -> None:
    with pytest.raises(ValidationError):
        Command(
            conversation_id=uuid4(),
            kind=CommandKind.INTERRUPT,
            idempotency_key="key",
            payload=SubmitTurnPayload(prompt="do work"),
            created_at=datetime.now(UTC),
        )
