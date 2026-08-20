"""Fail-closed behavior for shared live-gate event collection."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.live.helpers import LiveStream

from talktoharnesses.domain.enums import ApprovalDecision, InteractionKind
from talktoharnesses.domain.events import (
    ConversationEvent,
    InteractionRequestedPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
    TurnQueuedPayload,
    TurnStartedPayload,
)
from talktoharnesses.domain.models import ApprovalRequestPayload


def _event(conversation_id: UUID, sequence: int, payload: Any) -> ConversationEvent:
    return ConversationEvent(
        conversation_id=conversation_id,
        sequence=sequence,
        timestamp=datetime.now(UTC),
        type=payload.type,
        payload=payload,
    )


def _stream(items: list[ConversationEvent]) -> AsyncIterator[ConversationEvent]:
    async def _events() -> AsyncIterator[ConversationEvent]:
        for item in items:
            yield item

    return _events()


@pytest.mark.asyncio
async def test_collect_turn_answers_interaction_before_waiting_for_terminal() -> None:
    conversation_id = uuid4()
    turn_id = uuid4()
    interaction_id = uuid4()
    resolved: list[UUID] = []
    decisions: list[ApprovalDecision] = []

    async def on_event(event: ConversationEvent) -> None:
        payload = event.payload
        if isinstance(payload, InteractionRequestedPayload):
            resolved.append(payload.interaction_id)
            decisions.append(ApprovalDecision.ALLOW_ONCE)

    stream = LiveStream(
        _stream(
            [
                _event(
                    conversation_id,
                    1,
                    InteractionRequestedPayload(
                        turn_id=turn_id,
                        interaction_id=interaction_id,
                        kind=InteractionKind.APPROVAL,
                        request=ApprovalRequestPayload(),
                    ),
                ),
                _event(
                    conversation_id,
                    2,
                    TurnCompletedPayload(turn_id=turn_id, terminal_reason="end_turn"),
                ),
            ]
        ),
        on_event,
    )
    events = await stream.collect_turn(turn_id, timeout=1.0)
    assert [event.type for event in events] == ["interaction_requested", "turn_completed"]
    assert resolved == [interaction_id]
    assert decisions == [ApprovalDecision.ALLOW_ONCE]


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["failed", "completed_without_interaction"])
async def test_collect_turn_rejects_invalid_live_evidence(terminal: str) -> None:
    conversation_id = uuid4()
    turn_id = uuid4()
    if terminal == "failed":
        payload: Any = TurnFailedPayload(
            turn_id=turn_id,
            error_code="provider_error",
            message="failed",
        )
    else:
        payload = TurnCompletedPayload(turn_id=turn_id, terminal_reason="end_turn")

    async def on_event(_event: ConversationEvent) -> None:
        return None

    stream = LiveStream(_stream([_event(conversation_id, 1, payload)]), on_event)
    with pytest.raises(AssertionError):
        await stream.collect_turn(turn_id, timeout=1.0, min_interactions=1)


@pytest.mark.asyncio
async def test_collect_busy_turn_waits_for_target_turn_to_start() -> None:
    conversation_id = uuid4()
    turn_id = uuid4()
    command_id = uuid4()
    seen: list[str] = []
    progress_after: list[str] = []

    async def on_event(event: ConversationEvent) -> None:
        seen.append(event.type)

    async def on_progress() -> None:
        progress_after.append(seen[-1])

    stream = LiveStream(
        _stream(
            [
                _event(
                    conversation_id,
                    1,
                    TurnQueuedPayload(
                        turn_id=turn_id,
                        command_id=command_id,
                        prompt="wait",
                    ),
                ),
                _event(
                    conversation_id,
                    2,
                    TurnStartedPayload(turn_id=turn_id, command_id=command_id),
                ),
                _event(
                    conversation_id,
                    3,
                    TurnCompletedPayload(turn_id=turn_id, terminal_reason="end_turn"),
                ),
            ]
        ),
        on_event,
    )

    await stream.collect_busy_turn(
        turn_id,
        on_progress=on_progress,
        expected_terminal="turn_completed",
        timeout=1.0,
    )

    assert progress_after == ["turn_started"]
