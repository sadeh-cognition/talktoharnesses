"""Delta batch persistence failure regressions."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from talktoharnesses.application.delta_batcher import DeltaBatcher
from talktoharnesses.domain import append_events, new_conversation_state
from talktoharnesses.domain.events import ConversationEvent, ProviderWarningPayload
from talktoharnesses.domain.models import Command
from talktoharnesses.domain.transitions import ConversationState


@pytest.mark.asyncio
async def test_failed_flush_retains_the_pending_batch() -> None:
    now = datetime(2026, 8, 8, tzinfo=UTC)
    state = new_conversation_state(owner_id="owner", now=now)
    pending_state, events = append_events(
        state,
        now,
        [ProviderWarningPayload(message="keep me")],
    )
    calls: list[
        tuple[int, ConversationState, tuple[ConversationEvent, ...], tuple[Command, ...]]
    ] = []

    async def flush(
        base_version: int,
        flushed_state: ConversationState,
        flushed_events: Sequence[ConversationEvent],
        commands: Sequence[Command],
    ) -> Sequence[ConversationEvent]:
        calls.append((base_version, flushed_state, tuple(flushed_events), tuple(commands)))
        if len(calls) == 1:
            raise RuntimeError("transient")
        return flushed_events

    batcher = DeltaBatcher(conversation_id=uuid4(), flush=flush, interval_ms=0)
    with pytest.raises(RuntimeError, match="transient"):
        await batcher.add(base_version=0, state=pending_state, events=events)

    committed = await batcher.flush()

    assert tuple(committed) == events
    assert calls[0] == calls[1]
    assert batcher.state is None
