"""In-process committed-event broker contracts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

from talktoharnesses.application.broker import InProcessCommittedEventBroker
from talktoharnesses.application.publisher import ConversationWakeup
from talktoharnesses.domain.events import ConversationEvent, TurnStartedPayload


def _event(conversation_id: UUID, sequence: int) -> ConversationEvent:
    turn_id = uuid4()
    return ConversationEvent(
        conversation_id=conversation_id,
        sequence=sequence,
        timestamp=datetime(2026, 8, 8, tzinfo=UTC),
        type="turn_started",
        payload=TurnStartedPayload(turn_id=turn_id),
    )


@pytest.mark.asyncio
async def test_publish_wakes_subscriber() -> None:
    broker = InProcessCommittedEventBroker(poll_interval=10.0, keepalive_interval=30.0)
    await broker.start()
    cid = uuid4()
    gen = broker.subscribe(cid)
    # Start consumption in background after publish.
    received: list[int] = []

    async def consume_one() -> None:
        async for wakeup in gen:
            received.append(wakeup.sequence)
            break

    task = asyncio.create_task(consume_one())
    await asyncio.sleep(0)  # let subscribe register
    await broker.publish([_event(cid, 3), _event(cid, 5)])
    await asyncio.wait_for(task, timeout=2.0)
    assert received == [5]
    await broker.stop()


@pytest.mark.asyncio
async def test_wakeups_coalesce_to_highest_sequence() -> None:
    broker = InProcessCommittedEventBroker(poll_interval=10.0, keepalive_interval=30.0)
    await broker.start()
    cid = uuid4()
    sub = broker.subscribe(cid)
    waiter = asyncio.Event()
    sequences: list[int] = []

    async def consume() -> None:
        async for wakeup in sub:
            sequences.append(wakeup.sequence)
            waiter.set()
            if len(sequences) >= 1:
                break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    # Rapid publishes before consumer drains — subscriber holds max sequence.
    await broker.notify(cid, 1)
    await broker.notify(cid, 4)
    await broker.notify(cid, 2)
    await asyncio.wait_for(waiter.wait(), timeout=2.0)
    assert sequences[0] == 4
    await task
    await broker.stop()


@pytest.mark.asyncio
async def test_multiple_consumers() -> None:
    broker = InProcessCommittedEventBroker(poll_interval=10.0, keepalive_interval=30.0)
    await broker.start()
    cid = uuid4()
    results: list[int] = []

    async def one() -> None:
        async for w in broker.subscribe(cid):
            results.append(w.sequence)
            break

    t1 = asyncio.create_task(one())
    t2 = asyncio.create_task(one())
    await asyncio.sleep(0)
    await broker.publish([_event(cid, 7)])
    await asyncio.wait_for(asyncio.gather(t1, t2), timeout=2.0)
    assert results == [7, 7]
    await broker.stop()


@pytest.mark.asyncio
async def test_unsubscribe_on_generator_close() -> None:
    broker = InProcessCommittedEventBroker(poll_interval=10.0, keepalive_interval=30.0)
    await broker.start()
    cid = uuid4()
    gen = cast(AsyncGenerator[ConversationWakeup, None], broker.subscribe(cid).__aiter__())
    # Register by priming the generator.
    task = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0)
    assert cid in broker._subs  # pyright: ignore[reportPrivateUsage]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await gen.aclose()
    await asyncio.sleep(0)
    assert cid not in broker._subs  # pyright: ignore[reportPrivateUsage]
    await broker.stop()


@pytest.mark.asyncio
async def test_empty_publish_is_noop() -> None:
    broker = InProcessCommittedEventBroker()
    await broker.start()
    await broker.publish(())
    await broker.stop()


@pytest.mark.asyncio
async def test_start_stop_idempotent() -> None:
    broker = InProcessCommittedEventBroker()
    await broker.start()
    await broker.start()
    await broker.stop()
    await broker.stop()
