"""Claude normalizer thinking/tool/result path coverage."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from tth_types.enums import ApprovalDecision, ErrorCode, FileOperation, ToolOutcome
from tth_types.errors import DomainError
from tth_types.events import (
    AssistantMessageDeltaPayload,
    CostUpdatedPayload,
    InteractionRequestedPayload,
    ReasoningCompletedPayload,
    ReasoningDeltaPayload,
    ReasoningStartedPayload,
    ToolCompletedPayload,
    ToolRequestedPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
    TurnInterruptedPayload,
    UsageUpdatedPayload,
)
from tth_types.harness import ApprovalRequestPayload

from tth_claude.harness.normalizer import (
    ClaudeNormalizer,
    _as_int,  # pyright: ignore[reportPrivateUsage]
)
from tth_claude.harness.schemas import (
    ClaudeAssistantMessage,
    ClaudeResultMessage,
    ClaudeSystemMessage,
    ClaudeTextBlock,
    ClaudeThinkingBlock,
    ClaudeToolResultBlock,
    ClaudeToolUseBlock,
    ClaudeUserMessage,
)
from tth_claude.shared.questions import canonical_questions


def test_thinking_tool_result_and_terminal_variants() -> None:
    n = ClaudeNormalizer()
    n.set_redaction_patterns(("SECRET",))
    n.set_session("sess-1")
    n.import_seen(frozenset({"seen"}), frozenset({"off"}))
    native, offsets = n.export_seen()
    assert "seen" in native and "off" in offsets

    turn = uuid4()
    n.begin_turn(turn)
    assert n.on_message(ClaudeSystemMessage(subtype="init", data={})) == []

    events = n.on_message(
        ClaudeAssistantMessage(
            content=[
                ClaudeThinkingBlock(thinking="think SECRET"),
                ClaudeTextBlock(text="hello SECRET"),
                ClaudeToolUseBlock(id="t1", name="Bash", input={"cmd": "ls"}),
            ],
            model="claude",
            session_id="sess-1",
        )
    )
    assert any(isinstance(e, ReasoningStartedPayload) for e in events)
    assert any(isinstance(e, ReasoningDeltaPayload) and "***" in e.text for e in events)
    assert any(isinstance(e, AssistantMessageDeltaPayload) for e in events)
    assert any(isinstance(e, ToolRequestedPayload) for e in events)

    completed = n.on_message(
        ClaudeUserMessage(
            content=[
                ClaudeToolResultBlock(tool_use_id="t1", content="ok", is_error=False),
                ClaudeToolResultBlock(tool_use_id="unknown", content="x", is_error=True),
            ],
            session_id="sess-1",
        )
    )
    assert any(
        isinstance(e, ToolCompletedPayload) and e.outcome is ToolOutcome.SUCCESS for e in completed
    )

    terminal = n.on_message(
        ClaudeResultMessage(
            subtype="success",
            session_id="sess-1",
            is_error=False,
            stop_reason="end_turn",
            result="done",
            usage={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        )
    )
    assert any(isinstance(e, ReasoningCompletedPayload) for e in terminal)
    assert any(isinstance(e, UsageUpdatedPayload) for e in terminal)
    assert any(isinstance(e, TurnCompletedPayload) for e in terminal)

    n.begin_turn(uuid4())
    failed = n.on_message(
        ClaudeResultMessage(
            subtype="error",
            session_id="sess-1",
            is_error=True,
            errors=["boom"],
            stop_reason=None,
            result=None,
            usage=None,
        )
    )
    assert any(isinstance(e, TurnFailedPayload) and "boom" in e.message for e in failed)

    n.begin_turn(uuid4())
    n.request_interrupt()
    interrupted = n.on_message(
        ClaudeResultMessage(
            subtype="error",
            session_id="sess-1",
            is_error=True,
            errors=["interrupted"],
            stop_reason=None,
            result=None,
            usage=None,
        )
    )
    assert any(isinstance(e, TurnInterruptedPayload) for e in interrupted)

    n.begin_turn(uuid4())
    with pytest.raises(DomainError) as mismatch:
        n.on_message(
            ClaudeResultMessage(
                subtype="success",
                session_id="other",
                is_error=False,
                stop_reason="end_turn",
                result="x",
                usage=None,
            )
        )
    assert mismatch.value.code is ErrorCode.PROTOCOL_ERROR

    with pytest.raises(DomainError):
        ClaudeNormalizer().on_message(
            ClaudeResultMessage(
                subtype="success",
                session_id="s",
                is_error=False,
                stop_reason="end_turn",
                result="x",
                usage=None,
            )
        )

    assert _as_int(3) == 3  # pyright: ignore[reportPrivateUsage]
    assert _as_int("3") is None  # pyright: ignore[reportPrivateUsage]


def test_permission_request_mapping() -> None:
    n = ClaudeNormalizer()
    n.set_session("s")
    with pytest.raises(DomainError):
        n.on_permission_request(
            tool_name="Bash",
            tool_input={"command": "ls"},
            interaction_id=uuid4(),
        )
    n.begin_turn(uuid4())
    events = n.on_permission_request(
        tool_name="Bash",
        tool_input={"command": "ls"},
        interaction_id=uuid4(),
    )
    assert events
    assert ApprovalDecision.ALLOW_ONCE in events[0].request.available_decisions  # type: ignore[attr-defined]
    assert n.fail_active_turn(error_code="x", message="y")
    assert n.fail_active_turn(error_code="x", message="y") == []


@pytest.mark.parametrize(
    "tool_name,tool_input,path,operation",
    [
        ("Read", {"file_path": "/repo/AGENTS.md"}, "/repo/AGENTS.md", FileOperation.READ),
        ("Read", {}, None, FileOperation.READ),
        ("Read", {"file_path": 3}, None, FileOperation.READ),
        ("Glob", {"pattern": "**/*.py"}, ".", FileOperation.READ),
        ("Glob", {"pattern": "*.py", "path": "/repo"}, "/repo", FileOperation.READ),
        ("Glob", {"pattern": "../*"}, ".", None),
        ("Glob", {"pattern": "/outside/*"}, ".", None),
        ("Grep", {"pattern": "foo"}, ".", FileOperation.READ),
        ("Grep", {"pattern": "foo", "path": "/repo"}, "/repo", FileOperation.READ),
        ("Edit", {"file_path": "/repo/a.py"}, "/repo/a.py", FileOperation.MODIFY),
        ("Write", {"file_path": "/repo/a.py"}, "/repo/a.py", FileOperation.MODIFY),
        ("Bash", {"command": "cat AGENTS.md"}, None, None),
    ],
)
def test_file_permission_scope(
    tool_name: str, tool_input: dict[str, Any], path: str | None, operation: FileOperation | None
) -> None:
    normalizer = ClaudeNormalizer()
    normalizer.begin_turn(uuid4())
    event = normalizer.on_permission_request(
        tool_name=tool_name, tool_input=tool_input, interaction_id=uuid4()
    )[0]
    assert isinstance(event, InteractionRequestedPayload)
    assert isinstance(event.request, ApprovalRequestPayload)
    assert event.request.path == path
    assert event.request.operation == operation


def test_model_usage_is_aggregated_without_inventing_total() -> None:
    normalizer = ClaudeNormalizer()
    normalizer.set_session("sess-1")
    turn_id = uuid4()
    normalizer.begin_turn(turn_id)

    events = normalizer.on_message(
        ClaudeResultMessage(
            subtype="success",
            session_id="sess-1",
            usage={"input_tokens": 999, "output_tokens": 999},
            model_usage={
                "claude-a": {
                    "inputTokens": 10,
                    "outputTokens": 3,
                    "cacheReadInputTokens": 5,
                    "totalTokens": 13,
                },
                "claude-b": {
                    "inputTokens": 20,
                    "outputTokens": 4,
                    "cacheReadInputTokens": 6,
                },
            },
        )
    )

    usage = next(event for event in events if isinstance(event, UsageUpdatedPayload))
    assert usage.input_tokens == 30
    assert usage.output_tokens == 7
    assert usage.cached_input_tokens == 11
    assert usage.total_tokens is None


@pytest.mark.parametrize(
    "model_usage",
    [
        {"claude": {"input_tokens": 100, "output_tokens": 20}},
        {"claude": {"unrecognized": 100}},
    ],
)
def test_unrecognized_model_usage_falls_back_to_top_level_usage(
    model_usage: dict[str, object],
) -> None:
    normalizer = ClaudeNormalizer()
    normalizer.set_session("sess-1")
    normalizer.begin_turn(uuid4())

    events = normalizer.on_message(
        ClaudeResultMessage(
            subtype="success",
            session_id="sess-1",
            usage={"input_tokens": 10, "output_tokens": 3},
            model_usage=model_usage,
        )
    )

    usage = next(event for event in events if isinstance(event, UsageUpdatedPayload))
    assert usage.input_tokens == 10
    assert usage.output_tokens == 3


def test_unrecognized_usage_is_omitted() -> None:
    normalizer = ClaudeNormalizer()
    normalizer.set_session("sess-1")
    normalizer.begin_turn(uuid4())

    events = normalizer.on_message(
        ClaudeResultMessage(
            subtype="success",
            session_id="sess-1",
            usage={"unrecognized": 10},
            model_usage={"claude": {"unrecognized": 20}},
        )
    )

    assert not any(isinstance(event, UsageUpdatedPayload) for event in events)
    assert any(isinstance(event, TurnCompletedPayload) for event in events)


def test_structured_question_mapping() -> None:
    from tth_types.enums import InteractionKind

    n = ClaudeNormalizer()
    with pytest.raises(DomainError):
        n.on_question_request(questions=(), interaction_id=uuid4())
    n.begin_turn(uuid4())
    questions = canonical_questions([{"question": "Pick", "options": [{"label": "A"}]}])
    event = n.on_question_request(questions=questions, interaction_id=uuid4())[0]
    assert event.kind is InteractionKind.STRUCTURED_QUESTION  # type: ignore[attr-defined]
    assert event.request.questions == questions  # type: ignore[attr-defined]


@pytest.mark.parametrize("outcome", ["success", "error", "interrupted"])
@pytest.mark.parametrize("amount", [0.0, 0.123456789])
def test_reported_cost_precedes_terminal_event(outcome: str, amount: float) -> None:
    normalizer = ClaudeNormalizer()
    turn_id = uuid4()
    normalizer.begin_turn(turn_id)
    if outcome == "interrupted":
        normalizer.request_interrupt()

    events = normalizer.on_message(
        ClaudeResultMessage(
            subtype=outcome,
            session_id="session",
            is_error=outcome != "success",
            total_cost_usd=amount,
        )
    )

    assert events[:-1] == [CostUpdatedPayload(turn_id=turn_id, cost=str(amount), currency="USD")]
    terminal_type = {
        "success": TurnCompletedPayload,
        "error": TurnFailedPayload,
        "interrupted": TurnInterruptedPayload,
    }[outcome]
    assert isinstance(events[-1], terminal_type)


@pytest.mark.parametrize("amount", [None, -1.0, float("nan"), float("inf")])
def test_missing_or_invalid_cost_is_not_reported(amount: float | None) -> None:
    normalizer = ClaudeNormalizer()
    normalizer.begin_turn(uuid4())
    events = normalizer.on_message(
        ClaudeResultMessage(
            subtype="success",
            session_id="session",
            total_cost_usd=amount,
            usage={"input_tokens": 3, "output_tokens": 1},
        )
    )
    assert len(events) == 2
    assert isinstance(events[0], UsageUpdatedPayload)
    assert isinstance(events[1], TurnCompletedPayload)
