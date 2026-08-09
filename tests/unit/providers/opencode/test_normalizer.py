"""OpenCode normalizer mapping tests."""

from __future__ import annotations

from uuid import uuid4

import pytest

from talktoharnesses.domain.enums import ApprovalDecision, ErrorCode, InteractionKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import (
    AssistantMessageCompletedPayload,
    AssistantMessageDeltaPayload,
    AssistantMessageStartedPayload,
    InteractionRequestedPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
    TurnInterruptedPayload,
    TurnOutcomeUnknownPayload,
)
from talktoharnesses.domain.models import ApprovalRequestPayload
from talktoharnesses.providers.opencode.normalizer import OpenCodeNormalizer


def _delta(
    *,
    session_id: str = "sess-1",
    message_id: str = "m1",
    part_id: str = "p1",
    field: str = "text",
    delta: str = "hi",
) -> dict[str, object]:
    return {
        "type": "message.part.delta",
        "properties": {
            "sessionID": session_id,
            "messageID": message_id,
            "partID": part_id,
            "field": field,
            "delta": delta,
        },
    }


def _status(*, session_id: str = "sess-1", status: str) -> dict[str, object]:
    return {
        "type": "session.status",
        "properties": {
            "sessionID": session_id,
            "status": status,
        },
    }


def test_part_delta_emits_start_then_sequenced_deltas_with_redaction() -> None:
    n = OpenCodeNormalizer()
    n.set_redaction_patterns(("SECRET",))
    n.set_session("sess-1")
    turn = uuid4()
    n.begin_turn(turn)

    first = n.on_server_event(_delta(delta="hello SECRET"))
    assert isinstance(first[0], AssistantMessageStartedPayload)
    assert first[0].turn_id == turn
    assert isinstance(first[1], AssistantMessageDeltaPayload)
    assert first[1].sequence == 1
    assert first[1].text == "hello ***"

    second = n.on_server_event(_delta(delta=" world"))
    assert len(second) == 1
    assert isinstance(second[0], AssistantMessageDeltaPayload)
    assert second[0].sequence == 2
    assert second[0].message_id == first[0].message_id


def test_part_delta_dedupes_seen_offsets() -> None:
    n = OpenCodeNormalizer()
    n.set_session("sess-1")
    n.begin_turn(uuid4())
    first = n.on_server_event(_delta(delta="a"))
    assert len(first) == 2
    # Same sequence key is skipped after import/seen tracking.
    n.import_seen(frozenset(), frozenset({"m1:p1:1"}))
    n.begin_turn(uuid4())
    assert n.on_server_event(_delta(delta="b")) == []


def test_resync_and_no_turn_emit_empty() -> None:
    n = OpenCodeNormalizer()
    n.set_session("sess-1", resync=True)
    assert n.on_server_event(_delta()) == []

    n.set_session("sess-1", resync=False)
    assert n.on_server_event(_delta()) == []


def test_session_status_idle_completed_aborted_error() -> None:
    n = OpenCodeNormalizer()
    n.set_session("sess-1")

    turn = uuid4()
    n.begin_turn(turn)
    n.on_server_event(_delta(delta="done"))
    completed = n.on_server_event(_status(status="idle"))
    assert any(isinstance(e, AssistantMessageCompletedPayload) for e in completed)
    assert any(isinstance(e, TurnCompletedPayload) for e in completed)
    assert n._active_turn_id is None  # pyright: ignore[reportPrivateUsage]

    n.begin_turn(uuid4())
    done = n.on_server_event(_status(status="completed"))
    assert any(isinstance(e, TurnCompletedPayload) for e in done)

    n.begin_turn(uuid4())
    aborted = n.on_server_event(_status(status="aborted"))
    assert any(isinstance(e, TurnInterruptedPayload) for e in aborted)

    n.begin_turn(uuid4())
    failed = n.on_server_event(_status(status="error"))
    assert any(isinstance(e, TurnFailedPayload) for e in failed)


def test_child_sessions_accepted_via_parent_id() -> None:
    n = OpenCodeNormalizer()
    n.set_session("parent")
    n.begin_turn(uuid4())

    assert (
        n.on_server_event(
            {
                "type": "session.created",
                "properties": {"sessionID": "child-1", "parentID": "parent"},
            }
        )
        == []
    )
    assert n.accepts_session("child-1")

    events = n.on_server_event(_delta(session_id="child-1", delta="from-child"))
    assert any(isinstance(e, AssistantMessageDeltaPayload) for e in events)

    # Foreign session is ignored.
    assert n.on_server_event(_delta(session_id="other", delta="x")) == []


def test_unknown_event_type_is_unsupported() -> None:
    n = OpenCodeNormalizer()
    n.set_session("sess-1")
    with pytest.raises(DomainError) as exc:
        n.on_server_event({"type": "weird.event", "properties": {"sessionID": "sess-1"}})
    assert exc.value.code is ErrorCode.UNSUPPORTED_NATIVE_EVENT


def test_known_noise_event_types_are_ignored() -> None:
    n = OpenCodeNormalizer()
    n.set_session("sess-1")
    for event_type in (
        "server.connected",
        "message.updated",
        "message.part.updated",
        "session.updated",
        "session.diff",
        "todo.updated",
        "permission.asked",
    ):
        assert n.on_server_event({"type": event_type, "properties": {"sessionID": "sess-1"}}) == []


def test_on_permission_and_outcome_unknown() -> None:
    n = OpenCodeNormalizer()
    n.set_session("sess-1")
    turn = uuid4()
    interaction_id = uuid4()

    with pytest.raises(DomainError) as exc:
        n.on_permission(
            permission_id="p1",
            tool="bash",
            title="Run bash",
            interaction_id=interaction_id,
        )
    assert exc.value.code is ErrorCode.INVALID_STATE

    n.begin_turn(turn)
    events = n.on_permission(
        permission_id="p1",
        tool="bash",
        title="Run bash",
        interaction_id=interaction_id,
    )
    assert len(events) == 1
    assert isinstance(events[0], InteractionRequestedPayload)
    assert events[0].kind is InteractionKind.APPROVAL
    assert isinstance(events[0].request, ApprovalRequestPayload)
    assert events[0].request.available_decisions == (
        ApprovalDecision.ALLOW_ONCE,
        ApprovalDecision.DENY,
        ApprovalDecision.CANCEL,
    )

    unknown = n.on_outcome_unknown("stream lost")
    assert isinstance(unknown[-1], TurnOutcomeUnknownPayload)
    assert unknown[-1].message == "stream lost"
    assert n.on_outcome_unknown("no turn") == []


def test_part_delta_skips_non_text_and_empty() -> None:
    n = OpenCodeNormalizer()
    n.set_session("sess-1")
    n.begin_turn(uuid4())
    assert n.on_server_event(_delta(field="thinking", delta="x")) == []
    assert n.on_server_event(_delta(field="text", delta="")) == []


def test_export_import_seen() -> None:
    n = OpenCodeNormalizer()
    n.set_session("sess-1")
    n.begin_turn(uuid4())
    n.on_server_event(_delta(delta="a"))
    native_ids, offsets = n.export_seen()
    assert offsets
    other = OpenCodeNormalizer()
    other.import_seen(native_ids, offsets)
    other.set_session("sess-1")
    other.begin_turn(uuid4())
    assert other.on_server_event(_delta(delta="a")) == []
