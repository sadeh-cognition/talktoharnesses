"""Grok normalizer mapping tests."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from talktoharnesses.domain.enums import ApprovalDecision, ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import (
    AssistantMessageDeltaPayload,
    AssistantMessageStartedPayload,
    InteractionRequestedPayload,
    ToolRequestedPayload,
    TurnCompletedPayload,
    TurnInterruptedPayload,
    TurnOutcomeUnknownPayload,
)
from talktoharnesses.domain.models import ApprovalRequestPayload
from talktoharnesses.providers.adapter import HarnessInteractionRequest
from talktoharnesses.providers.grok.adapter import GrokAdapter
from talktoharnesses.providers.grok.compatibility import load_grok_compatibility
from talktoharnesses.providers.grok.normalizer import GrokNormalizer


def test_message_stream_and_terminal() -> None:
    n = GrokNormalizer()
    n.set_session("sess-1")
    turn = uuid4()
    n.begin_turn(turn)
    events = n.on_session_update(
        {
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "hi"},
            },
        }
    )
    assert any(isinstance(e, AssistantMessageStartedPayload) for e in events)
    assert any(isinstance(e, AssistantMessageDeltaPayload) for e in events)
    terminal = n.on_prompt_terminal("end_turn")
    assert any(isinstance(e, TurnCompletedPayload) for e in terminal)


def test_cancelled_stop_reason() -> None:
    n = GrokNormalizer()
    n.set_session("s")
    n.begin_turn(uuid4())
    terminal = n.on_prompt_terminal("cancelled")
    assert any(isinstance(e, TurnInterruptedPayload) for e in terminal)


def test_mismatched_session_is_protocol_error() -> None:
    n = GrokNormalizer()
    n.set_session("a")
    n.begin_turn(uuid4())
    with pytest.raises(DomainError) as exc:
        n.on_session_update(
            {
                "sessionId": "b",
                "update": {"sessionUpdate": "agent_message_chunk", "content": "x"},
            }
        )
    assert exc.value.code is ErrorCode.PROTOCOL_ERROR


def test_resync_mode_emits_no_events() -> None:
    n = GrokNormalizer()
    n.set_session("s", resync=True)
    events = n.on_session_update(
        {
            "sessionId": "s",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "m1",
                "content": "old",
            },
        }
    )
    assert events == []


def test_tool_arguments_are_redacted_before_emission() -> None:
    n = GrokNormalizer()
    n.set_redaction_patterns(("SECRET",))
    n.set_session("s")
    n.begin_turn(uuid4())
    events = n.on_session_update(
        {
            "sessionId": "s",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "tool-1",
                "rawInput": {"token": "prefix-SECRET-suffix"},
            },
        }
    )
    requested = next(event for event in events if isinstance(event, ToolRequestedPayload))
    assert requested.arguments == {"token": "prefix-[REDACTED]-suffix"}


def test_permission_decision_selects_an_advertised_option_id() -> None:
    n = GrokNormalizer()
    options = [{"optionId": "native-yes", "kind": "allow_once"}]

    assert n.map_approval_decision(ApprovalDecision.ALLOW_ONCE, options) == {
        "outcome": {"outcome": "selected", "optionId": "native-yes"}
    }
    assert n.map_approval_decision(ApprovalDecision.ALLOW_SESSION, options) == {
        "outcome": {"outcome": "cancelled"}
    }


def test_permission_advertises_cancel_even_without_selected_option() -> None:
    n = GrokNormalizer()
    n.set_session("s")
    n.begin_turn(uuid4())

    event = n.on_permission_request({}, interaction_id=uuid4())[0]

    assert isinstance(event, InteractionRequestedPayload)
    assert isinstance(event.request, ApprovalRequestPayload)
    assert event.request.available_decisions == (ApprovalDecision.CANCEL,)


@pytest.mark.asyncio
async def test_adapter_wraps_permission_correlation_in_private_envelope() -> None:
    adapter = GrokAdapter()
    adapter._normalizer.set_session("s")  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.begin_turn(uuid4())  # pyright: ignore[reportPrivateUsage]
    request = SimpleNamespace(
        id="rpc-7",
        params={
            "sessionId": "s",
            "toolCall": {"toolCallId": "tool-3"},
            "options": [],
        },
    )

    await adapter._on_permission_request(request)  # pyright: ignore[reportPrivateUsage]
    event = adapter._event_q.get_nowait()  # pyright: ignore[reportPrivateUsage]

    assert isinstance(event, HarnessInteractionRequest)
    assert event.provider_correlation == {
        "json_rpc_request_id": "rpc-7",
        "tool_call_id": "tool-3",
        "native_session_id": "s",
    }


def test_tool_output_delta_sequences_increment() -> None:
    from talktoharnesses.domain.events import ToolOutputDeltaPayload

    n = GrokNormalizer()
    n.set_session("s")
    n.begin_turn(uuid4())
    first = n.on_session_update(
        {
            "sessionId": "s",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "t1",
                "content": "hello",
            },
        }
    )
    second = n.on_session_update(
        {
            "sessionId": "s",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "t1",
                "content": " world",
            },
        }
    )
    deltas = [e for e in (*first, *second) if isinstance(e, ToolOutputDeltaPayload)]
    assert [d.sequence for d in deltas] == [1, 2]


def test_delivered_protocol_fault_is_outcome_unknown() -> None:
    n = GrokNormalizer()
    n.set_session("s")
    n.begin_turn(uuid4())

    events = n.on_prompt_outcome_unknown("malformed notification")

    assert isinstance(events[-1], TurnOutcomeUnknownPayload)


def test_initialize_requires_pinned_identity_and_resume_capability() -> None:
    adapter = GrokAdapter()
    adapter._release = (  # pyright: ignore[reportPrivateUsage]
        load_grok_compatibility().releases[0]
    )
    with pytest.raises(DomainError) as exc:
        adapter._validate_initialize_identity({})  # pyright: ignore[reportPrivateUsage]
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE

    adapter._validate_initialize_identity(  # pyright: ignore[reportPrivateUsage]
        {
            "agentInfo": {"name": "grok", "version": "1.0.0"},
            "agentCapabilities": {"loadSession": True},
        }
    )
