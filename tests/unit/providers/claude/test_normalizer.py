"""Claude normalizer thinking/tool/result path coverage."""

from __future__ import annotations

from uuid import uuid4

import pytest

from talktoharnesses.domain.enums import ApprovalDecision, ErrorCode, ToolOutcome
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import (
    AssistantMessageDeltaPayload,
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
from talktoharnesses.domain.questions import canonical_questions
from talktoharnesses.providers.claude.normalizer import (
    ClaudeNormalizer,
    _as_int,  # pyright: ignore[reportPrivateUsage]
)
from talktoharnesses.providers.claude.schemas import (
    ClaudeAssistantMessage,
    ClaudeResultMessage,
    ClaudeSystemMessage,
    ClaudeTextBlock,
    ClaudeThinkingBlock,
    ClaudeToolResultBlock,
    ClaudeToolUseBlock,
)


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
        ClaudeAssistantMessage(
            content=[
                ClaudeToolResultBlock(tool_use_id="t1", content="ok", is_error=False),
                ClaudeToolResultBlock(tool_use_id="unknown", content="x", is_error=True),
            ],
            model="claude",
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


def test_structured_question_mapping() -> None:
    from talktoharnesses.domain.enums import InteractionKind

    n = ClaudeNormalizer()
    with pytest.raises(DomainError):
        n.on_question_request(questions=(), interaction_id=uuid4())
    n.begin_turn(uuid4())
    questions = canonical_questions([{"question": "Pick", "options": [{"label": "A"}]}])
    event = n.on_question_request(questions=questions, interaction_id=uuid4())[0]
    assert event.kind is InteractionKind.STRUCTURED_QUESTION  # type: ignore[attr-defined]
    assert event.request.questions == questions  # type: ignore[attr-defined]
