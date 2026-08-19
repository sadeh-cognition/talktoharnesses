"""Direct materialize_projections coverage for event projection branches."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from asgiref.sync import async_to_sync
from tests.phase8_fixtures import idle_state

from talktoharnesses.django.materialize import materialize_projections
from talktoharnesses.django.models import (
    MessageRecord,
    PlanRecord,
    ReasoningRecord,
    ToolRecord,
    TurnRecord,
    UsageRecordRow,
)
from talktoharnesses.django.persistence import DjangoPersistence
from talktoharnesses.domain.enums import ToolOutcome, TurnStatus
from talktoharnesses.domain.events import (
    AssistantMessageCompletedPayload,
    AssistantMessageDeltaPayload,
    AssistantMessageStartedPayload,
    ConversationEvent,
    CostUpdatedPayload,
    PlanCreatedPayload,
    PlanUpdatedPayload,
    ReasoningCompletedPayload,
    ReasoningDeltaPayload,
    ReasoningStartedPayload,
    ToolCompletedPayload,
    ToolFailedPayload,
    ToolOutputDeltaPayload,
    ToolRequestedPayload,
    TurnCancelledPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
    TurnInterruptedPayload,
    TurnOutcomeUnknownPayload,
    UsageUpdatedPayload,
)
from talktoharnesses.domain.models import PlanItem


@pytest.mark.django_db(transaction=True)
def test_materialize_reasoning_tool_plan_and_terminal_branches() -> None:
    now = datetime(2026, 8, 8, tzinfo=UTC)
    persistence = DjangoPersistence()
    state = idle_state()
    async_to_sync(persistence.save_snapshot)(state)
    cid = state.conversation.id
    turn_id = uuid4()
    message_id = uuid4()
    reasoning_id = uuid4()
    tool_id = uuid4()
    plan_id = uuid4()

    # Missing aggregate short-circuits.
    materialize_projections(
        idle_state(),
        (
            ConversationEvent(
                conversation_id=uuid4(),
                sequence=1,
                timestamp=now,
                type="turn_completed",
                payload=TurnCompletedPayload(
                    turn_id=turn_id, terminal_reason="end_turn", has_assistant_message=False
                ),
            ),
        ),
    )

    seq = 1

    def ev(type_: str, payload: object) -> ConversationEvent:
        nonlocal seq
        event = ConversationEvent(
            conversation_id=cid,
            sequence=seq,
            timestamp=now,
            type=type_,
            payload=payload,  # type: ignore[arg-type]
        )
        seq += 1
        return event

    events = (
        ev(
            "assistant_message_started",
            AssistantMessageStartedPayload(turn_id=turn_id, message_id=message_id),
        ),
        ev(
            "assistant_message_delta",
            AssistantMessageDeltaPayload(
                turn_id=turn_id, message_id=message_id, sequence=1, text="hi"
            ),
        ),
        ev(
            "assistant_message_completed",
            AssistantMessageCompletedPayload(turn_id=turn_id, message_id=message_id, text="hi"),
        ),
        ev(
            "reasoning_started",
            ReasoningStartedPayload(turn_id=turn_id, reasoning_id=reasoning_id),
        ),
        ev(
            "reasoning_delta",
            ReasoningDeltaPayload(turn_id=turn_id, reasoning_id=reasoning_id, text="think"),
        ),
        ev(
            "reasoning_completed",
            ReasoningCompletedPayload(turn_id=turn_id, reasoning_id=reasoning_id, text="think"),
        ),
        ev(
            "tool_requested",
            ToolRequestedPayload(turn_id=turn_id, tool_id=tool_id, tool_name="shell", arguments={}),
        ),
        ev(
            "tool_output_delta",
            ToolOutputDeltaPayload(turn_id=turn_id, tool_id=tool_id, sequence=1, text="out"),
        ),
        ev(
            "tool_completed",
            ToolCompletedPayload(
                turn_id=turn_id,
                tool_id=tool_id,
                tool_name="shell",
                outcome=ToolOutcome.SUCCESS,
                output_tail="out",
            ),
        ),
        ev(
            "tool_failed",
            ToolFailedPayload(turn_id=turn_id, tool_id=uuid4(), tool_name="shell", message="boom"),
        ),
        ev(
            "plan_created",
            PlanCreatedPayload(
                turn_id=turn_id,
                plan_id=plan_id,
                items=(PlanItem(id="1", title="a"),),
            ),
        ),
        ev(
            "plan_updated",
            PlanUpdatedPayload(
                turn_id=turn_id,
                plan_id=plan_id,
                items=(PlanItem(id="1", title="b"),),
            ),
        ),
        ev(
            "turn_interrupted",
            TurnInterruptedPayload(turn_id=turn_id, reason="cancelled"),
        ),
    )
    materialize_projections(state, events)
    assert MessageRecord.objects.filter(message_id=message_id).exists()
    assert ReasoningRecord.objects.filter(reasoning_id=reasoning_id).exists()
    assert ToolRecord.objects.filter(tool_id=tool_id).exists()
    assert PlanRecord.objects.filter(plan_id=plan_id).exists()

    for payload, expected in (
        (
            TurnFailedPayload(turn_id=uuid4(), error_code="x", message="fail"),
            TurnStatus.FAILED,
        ),
        (
            TurnOutcomeUnknownPayload(turn_id=uuid4(), message="unk"),
            TurnStatus.OUTCOME_UNKNOWN,
        ),
        (TurnCancelledPayload(turn_id=uuid4()), TurnStatus.INTERRUPTED),
        (
            TurnCompletedPayload(
                turn_id=uuid4(), terminal_reason="end_turn", has_assistant_message=False
            ),
            TurnStatus.COMPLETED,
        ),
    ):
        turn = payload.turn_id
        TurnRecord.objects.create(
            turn_id=turn,
            conversation_id=cid,
            status=TurnStatus.RUNNING.value,
            created_at=now,
            started_at=now,
            order_index=seq,
        )
        type_name = {
            TurnFailedPayload: "turn_failed",
            TurnOutcomeUnknownPayload: "turn_outcome_unknown",
            TurnCancelledPayload: "turn_cancelled",
            TurnCompletedPayload: "turn_completed",
        }[type(payload)]
        materialize_projections(
            state,
            (
                ConversationEvent(
                    conversation_id=cid,
                    sequence=seq,
                    timestamp=now,
                    type=type_name,
                    payload=payload,
                ),
            ),
        )
        seq += 1
        row = TurnRecord.objects.get(turn_id=turn)
        assert row.status == expected.value


@pytest.mark.django_db(transaction=True)
def test_materialize_usage_preserves_cost_currency() -> None:
    now = datetime(2026, 8, 19, tzinfo=UTC)
    persistence = DjangoPersistence()
    state = idle_state()
    async_to_sync(persistence.save_snapshot)(state)
    conversation_id = state.conversation.id
    turn_id = uuid4()
    events = (
        ConversationEvent(
            conversation_id=conversation_id,
            sequence=1,
            timestamp=now,
            type="usage_updated",
            payload=UsageUpdatedPayload(
                turn_id=turn_id,
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                cached_input_tokens=2,
            ),
        ),
        ConversationEvent(
            conversation_id=conversation_id,
            sequence=2,
            timestamp=now,
            type="cost_updated",
            payload=CostUpdatedPayload(
                turn_id=turn_id,
                cost="0.0123",
                currency="USD",
            ),
        ),
    )

    materialize_projections(state, events)

    usage = UsageRecordRow.objects.get(turn_id=turn_id)
    assert usage.input_tokens == 10
    assert usage.cached_input_tokens == 2
    assert usage.cost == "0.0123"
    assert usage.currency == "USD"
