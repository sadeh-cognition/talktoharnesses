"""OpenCode normalizer mapping tests."""

from __future__ import annotations

from uuid import uuid4

import pytest
from tth_types.enums import ApprovalDecision, ErrorCode, InteractionKind, ToolOutcome
from tth_types.errors import DomainError
from tth_types.events import (
    AssistantMessageCompletedPayload,
    AssistantMessageDeltaPayload,
    AssistantMessageStartedPayload,
    CostUpdatedPayload,
    InteractionRequestedPayload,
    ToolCompletedPayload,
    ToolFailedPayload,
    ToolRequestedPayload,
    ToolStartedPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
    TurnInterruptedPayload,
    TurnOutcomeUnknownPayload,
    UsageUpdatedPayload,
)
from tth_types.harness import CANONICAL_TOOL_TAIL_BYTES, ApprovalRequestPayload

from tth_opencode.harness.normalizer import OpenCodeNormalizer


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


def _step(
    part_id: str,
    *,
    session_id: str = "sess-1",
    input_tokens: int = 10,
    output_tokens: int = 2,
    cached_tokens: int = 4,
    total_tokens: int | None = 12,
) -> dict[str, object]:
    tokens: dict[str, object] = {
        "input": input_tokens,
        "output": output_tokens,
        "reasoning": 1,
        "cache": {"read": cached_tokens, "write": 3},
    }
    if total_tokens is not None:
        tokens["total"] = total_tokens
    return {
        "id": part_id,
        "sessionID": session_id,
        "messageID": f"message-{part_id}",
        "type": "step-finish",
        "reason": "stop",
        "cost": 0.1,
        "tokens": tokens,
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


def _tool_event(status: str, *, session_id: str = "sess-1", **state: object) -> dict[str, object]:
    return {
        "id": f"evt_{status}",
        "type": "message.part.updated",
        "properties": {
            "part": {
                "id": "part-read",
                "sessionID": session_id,
                "messageID": "message-read",
                "type": "tool",
                "callID": "call-read",
                "tool": "read",
                "state": {"status": status, "input": {"filePath": "/tmp/file"}, **state},
            }
        },
    }


def test_tool_lifecycle_redacts_and_deduplicates_updates() -> None:
    n = OpenCodeNormalizer()
    n.set_session("sess-1")
    n.set_redaction_patterns(("SECRET",))
    turn = uuid4()
    n.begin_turn(turn)
    assert n.on_server_event(_tool_event("pending")) == []
    running = _tool_event("running", input={"paths": ["/SECRET", {"token": "SECRET"}], "limit": 5})
    requested, started = n.on_server_event(running)
    assert isinstance(requested, ToolRequestedPayload)
    assert requested.arguments == {"paths": ["/***", {"token": "***"}], "limit": 5}
    assert isinstance(started, ToolStartedPayload)
    assert started.turn_id == turn
    assert started.tool_name == "read"
    assert requested.tool_id == started.tool_id
    assert n.on_server_event(running) == []

    completed = _tool_event("completed", output="文" * CANONICAL_TOOL_TAIL_BYTES + "SECRET")
    result = n.on_server_event(completed)
    assert len(result) == 1
    assert isinstance(result[0], ToolCompletedPayload)
    assert result[0].tool_id == started.tool_id
    assert result[0].outcome is ToolOutcome.SUCCESS
    assert result[0].output_tail.endswith("***")
    assert len(result[0].output_tail.encode()) <= CANONICAL_TOOL_TAIL_BYTES
    assert n.on_server_event(completed) == []
    assert n.on_server_event(running) == []

    restored = OpenCodeNormalizer()
    restored.set_session("sess-1")
    restored.begin_turn(turn)
    restored.import_seen(*n.export_seen())
    assert restored.on_server_event(completed) == []


@pytest.mark.parametrize("status", ["completed", "error"])
def test_tool_terminal_snapshot_includes_start_when_running_update_was_missed(status: str) -> None:
    n = OpenCodeNormalizer()
    n.set_session("sess-1")
    n.set_redaction_patterns(("SECRET",))
    n.begin_turn(uuid4())
    event = _tool_event(status, output="file contents", error="Cannot read SECRET")
    requested, started, terminal = n.on_server_event(event)
    assert isinstance(requested, ToolRequestedPayload)
    assert isinstance(started, ToolStartedPayload)
    assert isinstance(terminal, (ToolCompletedPayload, ToolFailedPayload))
    assert requested.tool_id == started.tool_id == terminal.tool_id
    if status == "error":
        assert isinstance(terminal, ToolFailedPayload)
        assert terminal.message == "Cannot read ***"
    else:
        assert isinstance(terminal, ToolCompletedPayload)
        assert terminal.output_tail == "file contents"
    assert n.on_server_event(event) == []


def test_tool_updates_filter_nested_session_ids_and_resync() -> None:
    n = OpenCodeNormalizer()
    n.set_session("sess-1")
    assert n.on_server_event(_tool_event("running")) == []
    n.begin_turn(uuid4())
    n.set_session("sess-1", resync=True)
    assert n.on_server_event(_tool_event("running")) == []
    n.set_session("sess-1")
    assert n.on_server_event(_tool_event("running", session_id="foreign")) == []
    n.on_server_event(
        {"type": "session.created", "properties": {"info": {"id": "child", "parentID": "sess-1"}}}
    )
    child = n.on_server_event(_tool_event("running", session_id="child"))
    parent = n.on_server_event(_tool_event("running"))
    assert isinstance(child[1], ToolStartedPayload)
    assert isinstance(parent[1], ToolStartedPayload)
    assert child[1].tool_id != parent[1].tool_id


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
                "properties": {"info": {"id": "child-1", "parentID": "parent"}},
            }
        )
        == []
    )
    assert n.accepts_session("child-1")

    events = n.on_server_event(_delta(session_id="child-1", delta="from-child"))
    assert any(isinstance(e, AssistantMessageDeltaPayload) for e in events)

    # Foreign session is ignored.
    assert n.on_server_event(_delta(session_id="other", delta="x")) == []


@pytest.mark.parametrize(
    ("event_type", "status"),
    [
        ("session.idle", None),
        ("session.status", {"type": "idle"}),
    ],
)
def test_child_session_terminal_does_not_complete_parent(
    event_type: str,
    status: object,
) -> None:
    normalizer = OpenCodeNormalizer()
    normalizer.set_session("parent")
    normalizer.begin_turn(uuid4())
    normalizer.on_server_event(
        {
            "type": "session.created",
            "properties": {"info": {"id": "child-1", "parentID": "parent"}},
        }
    )
    properties: dict[str, object] = {"sessionID": "child-1"}
    if status is not None:
        properties["status"] = status

    assert normalizer.on_server_event({"type": event_type, "properties": properties}) == []
    parent_terminal = normalizer.on_server_event(_status(session_id="parent", status="idle"))
    assert any(isinstance(event, TurnCompletedPayload) for event in parent_terminal)


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
        "catalog.updated",
        "integration.updated",
        "plugin.added",
        "reference.updated",
        "message.updated",
        "message.part.updated",
        "session.created",
        "session.updated",
        "session.diff",
        "todo.updated",
        "permission.asked",
    ):
        assert n.on_server_event({"type": event_type, "properties": {"sessionID": "sess-1"}}) == []


def test_step_usage_aggregates_unique_parent_and_child_parts_before_terminal() -> None:
    normalizer = OpenCodeNormalizer()
    normalizer.set_session("sess-1")
    turn_id = uuid4()
    normalizer.begin_turn(turn_id, root_message_id="root-message")

    parent = {
        "type": "message.part.updated",
        "properties": {"part": _step("step-1")},
    }
    normalizer.on_server_event(parent)
    normalizer.on_server_event(parent)
    normalizer.on_server_event(
        {
            "type": "session.created",
            "properties": {"info": {"id": "child-1", "parentID": "sess-1"}},
        }
    )
    normalizer.on_server_event(
        {
            "type": "message.part.updated",
            "properties": {
                "part": _step(
                    "step-2",
                    session_id="child-1",
                    input_tokens=20,
                    output_tokens=5,
                    cached_tokens=6,
                    total_tokens=None,
                )
            },
        }
    )

    terminal = normalizer.on_server_event(_status(status="idle"))
    usage = next(event for event in terminal if isinstance(event, UsageUpdatedPayload))
    assert usage.input_tokens == 30
    assert usage.output_tokens == 7
    assert usage.cached_input_tokens == 10
    assert usage.total_tokens is None
    assert terminal.index(usage) < len(terminal) - 1
    cost = next(event for event in terminal if isinstance(event, CostUpdatedPayload))
    assert cost.cost == "0.2"
    assert cost.currency == "USD"
    assert cost.turn_id == turn_id
    assert terminal.index(cost) < len(terminal) - 1


def test_history_usage_starts_at_current_root_message() -> None:
    normalizer = OpenCodeNormalizer()
    normalizer.set_session("sess-1")
    normalizer.begin_turn(uuid4(), root_message_id="root-message")
    normalizer.on_message_history(
        "sess-1",
        [
            {"info": {"id": "old"}, "parts": [_step("old-step", input_tokens=999)]},
            {"info": {"id": "root-message"}, "parts": []},
            {"info": {"id": "answer"}, "parts": [_step("current-step")]},
        ],
        after_message_id="root-message",
    )

    terminal = normalizer.on_server_event(_status(status="idle"))
    usage = next(event for event in terminal if isinstance(event, UsageUpdatedPayload))
    assert usage.input_tokens == 10
    cost = next(event for event in terminal if isinstance(event, CostUpdatedPayload))
    assert cost.cost == "0.1"

    # Resuming a session starts a new turn's total, without billing old history again.
    normalizer.begin_turn(uuid4(), root_message_id="next-root")
    free_step = {**_step("free-step"), "cost": 0}
    normalizer.on_message_history(
        "sess-1",
        [
            {"info": {"id": "answer"}, "parts": [_step("current-step")]},
            {"info": {"id": "next-root"}, "parts": []},
            {"info": {"id": "next-answer"}, "parts": [free_step]},
        ],
        after_message_id="next-root",
    )
    terminal = normalizer.on_server_event(_status(status="idle"))
    cost = next(event for event in terminal if isinstance(event, CostUpdatedPayload))
    assert cost.cost == "0.0"


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
