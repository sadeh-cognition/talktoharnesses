"""SSE replay, reconciliation, and soft-delete regressions."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

from talktoharnesses.application.publisher import ConversationWakeup
from talktoharnesses.application.service import TalkToHarnessesService
from talktoharnesses.django.api.sse import (
    _bounded_replay,  # pyright: ignore[reportPrivateUsage]
    iter_sse,
)
from talktoharnesses.domain import new_conversation_state
from talktoharnesses.domain.events import (
    ConversationEvent,
    ConversationMetadataChangedPayload,
    TurnStartedPayload,
)
from talktoharnesses.domain.models import ConversationSnapshot


def _event(conversation_id: UUID, sequence: int) -> ConversationEvent:
    return ConversationEvent(
        conversation_id=conversation_id,
        sequence=sequence,
        timestamp=datetime(2026, 8, 8, tzinfo=UTC),
        type="turn_started",
        payload=TurnStartedPayload(turn_id=uuid4()),
    )


class _Service:
    def __init__(
        self,
        conversation_id: UUID,
        *,
        high_waters: Sequence[int],
        events: Sequence[ConversationEvent] = (),
        wakeups: Sequence[int] = (),
        defer_first_replay: bool = False,
    ) -> None:
        self.conversation_id = conversation_id
        self.high_waters = list(high_waters)
        self.events = tuple(events)
        self.wakeups = tuple(wakeups)
        self.defer_first_replay = defer_first_replay
        self.replay_calls = 0
        state = new_conversation_state(
            owner_id="owner",
            now=datetime(2026, 8, 8, tzinfo=UTC),
            conversation_id=conversation_id,
        )
        from talktoharnesses.domain.models import ConversationDetail

        self.snapshot = ConversationSnapshot(
            sequence=max(high_waters, default=0),
            detail=ConversationDetail(conversation=state.conversation),
        )
        self.publisher = self

    async def get_conversation(self, owner_id: str, conversation_id: UUID) -> object:
        return object()

    async def get_stream_high_water_sequence(
        self,
        owner_id: str,
        conversation_id: UUID,
    ) -> int:
        if len(self.high_waters) > 1:
            return self.high_waters.pop(0)
        return self.high_waters[0]

    async def replay_stream_events(
        self,
        owner_id: str,
        conversation_id: UUID,
        *,
        after_sequence: int,
        event_count_limit: int,
        byte_limit: int,
    ) -> Sequence[ConversationEvent]:
        self.replay_calls += 1
        if self.defer_first_replay and self.replay_calls == 1:
            return ()
        return tuple(event for event in self.events if event.sequence > after_sequence)

    async def get_snapshot(
        self,
        owner_id: str,
        conversation_id: UUID,
    ) -> ConversationSnapshot:
        return self.snapshot

    def subscribe(self, conversation_id: UUID) -> AsyncIterator[ConversationWakeup]:
        async def generate() -> AsyncIterator[ConversationWakeup]:
            for sequence in self.wakeups:
                yield ConversationWakeup(conversation_id=conversation_id, sequence=sequence)

        return generate()


@pytest.mark.asyncio
@pytest.mark.parametrize("events", [(), ("prefix",)])
async def test_incomplete_replay_uses_snapshot_even_below_count_cap(
    events: tuple[str, ...],
) -> None:
    conversation_id = uuid4()
    replay = (_event(conversation_id, 1),) if events else ()
    service = _Service(conversation_id, high_waters=(2,), events=replay)

    frames, sequence, deleted = await _bounded_replay(  # pyright: ignore[reportPrivateUsage]
        cast(TalkToHarnessesService, service),
        owner_id="owner",
        conversation_id=conversation_id,
        after=0,
    )

    assert "event: snapshot" in frames[0]
    assert sequence == 2
    assert deleted is False


@pytest.mark.asyncio
async def test_stale_wakeup_still_reconciles_persistence() -> None:
    conversation_id = uuid4()
    service = _Service(
        conversation_id,
        high_waters=(0, 1),
        events=(_event(conversation_id, 1),),
        wakeups=(0,),
        defer_first_replay=True,
    )
    stream = cast(
        AsyncGenerator[str, None],
        iter_sse(
            cast(TalkToHarnessesService, service),
            owner_id="owner",
            conversation_id=conversation_id,
            last_event_id=0,
        ),
    )

    assert "event: sync" in await anext(stream)
    assert "event: turn_started" in await anext(stream)
    await stream.aclose()


@pytest.mark.asyncio
async def test_existing_stream_receives_soft_delete_event_then_closes() -> None:
    conversation_id = uuid4()
    deleted = ConversationEvent(
        conversation_id=conversation_id,
        sequence=1,
        timestamp=datetime(2026, 8, 8, tzinfo=UTC),
        type="conversation_metadata_changed",
        payload=ConversationMetadataChangedPayload(
            deleted_at=datetime(2026, 8, 8, tzinfo=UTC)
        ),
    )
    service = _Service(conversation_id, high_waters=(1,), events=(deleted,))
    stream = iter_sse(
        cast(TalkToHarnessesService, service),
        owner_id="owner",
        conversation_id=conversation_id,
        last_event_id=0,
    )

    assert "event: conversation_metadata_changed" in await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
