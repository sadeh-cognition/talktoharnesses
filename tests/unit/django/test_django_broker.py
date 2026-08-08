"""Django event broker wiring (SQLite path in CI)."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from talktoharnesses.django.broker import DjangoCommittedEventBroker
from talktoharnesses.domain.events import ConversationEvent, TurnStartedPayload


def _event(conversation_id: UUID, sequence: int) -> ConversationEvent:
    return ConversationEvent(
        conversation_id=conversation_id,
        sequence=sequence,
        timestamp=datetime(2026, 8, 8, tzinfo=UTC),
        type="turn_started",
        payload=TurnStartedPayload(turn_id=uuid4()),
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_django_broker_sqlite_publish_and_subscribe() -> None:
    broker = DjangoCommittedEventBroker(poll_interval=10.0, keepalive_interval=30.0)
    await broker.start()
    cid = uuid4()
    got: list[int] = []

    async def consume() -> None:
        async for w in broker.subscribe(cid):
            got.append(w.sequence)
            break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await broker.publish([_event(cid, 2), _event(cid, 9)])
    await asyncio.wait_for(task, timeout=2.0)
    assert got == [9]
    assert broker.high_water(cid) == 9
    await broker.stop()
    await broker.stop()


@pytest.mark.asyncio
async def test_postgres_notify_runs_off_the_event_loop_thread() -> None:
    broker = DjangoCommittedEventBroker()
    conversation_id = uuid4()
    event_loop_thread = threading.get_ident()
    notify_threads: list[int] = []

    def notify(by_conversation: dict[UUID, int]) -> None:
        assert by_conversation == {conversation_id: 1}
        notify_threads.append(threading.get_ident())

    with (
        patch("talktoharnesses.django.broker.connection") as db_connection,
        patch.object(broker, "_pg_notify", side_effect=notify),
    ):
        db_connection.vendor = "postgresql"
        await broker.publish([_event(conversation_id, 1)])

    assert notify_threads
    assert notify_threads[0] != event_loop_thread
