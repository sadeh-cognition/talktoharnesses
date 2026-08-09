"""Fixed release performance budgets (SQLite file-backed; PostgreSQL in CI)."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from asgiref.sync import sync_to_async
from django.db import connection
from django.test.utils import CaptureQueriesContext
from tests.performance.helpers import measure_p95_ns, percentile_ns
from tests.phase8_fixtures import NOW, idle_state

from talktoharnesses.application.broker import InProcessCommittedEventBroker
from talktoharnesses.application.publisher import ConversationWakeup
from talktoharnesses.application.service import TalkToHarnessesService
from talktoharnesses.django.models import (
    ConversationAggregate,
    ConversationEventRecord,
    SearchDocument,
)
from talktoharnesses.django.persistence import DjangoPersistence
from talktoharnesses.domain.events import ConversationEvent, ConversationTitleUpdatedPayload
from talktoharnesses.domain.models import ConversationSearchHit, ConversationShell, Page
from talktoharnesses.providers.registry import AdapterRegistry
from talktoharnesses.runtime.manager import RuntimeManager

OWNER = "perf-owner"
OTHER = "perf-other"
MS = 1_000_000
LIST_BUDGET_NS = 250 * MS
SEARCH_BUDGET_NS = 500 * MS
SUBMIT_BUDGET_NS = 250 * MS
REPLAY_BUDGET_NS = 2_000 * MS
SSE_SQLITE_BUDGET_NS = 500 * MS
SSE_POSTGRES_BUDGET_NS = 250 * MS


def _empty_state_json(owner_id: str, conversation_id: object) -> dict[str, object]:
    return {
        "conversation": {
            "id": str(conversation_id),
            "owner_id": owner_id,
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "status": "idle",
            "version": 0,
        },
        "binding": None,
        "turns": {},
        "commands": {},
        "messages": {},
        "interactions": {},
        "activities": {},
        "processes": {},
        "pending_interactions": {},
        "approval_rules": {},
        "seen_native_ids": [],
        "seen_stream_offsets": [],
    }


def _seed_shells(count: int, *, owner_id: str, title_prefix: str) -> None:
    rows: list[ConversationAggregate] = []
    docs: list[SearchDocument] = []
    for index in range(count):
        conversation_id = uuid4()
        updated = NOW + timedelta(seconds=index)
        rows.append(
            ConversationAggregate(
                conversation_id=conversation_id,
                owner_id=owner_id,
                version=0,
                next_event_sequence=1,
                updated_at=updated,
                title=f"{title_prefix}-{index:05d}",
                status="idle",
                harness_kind="codex",
                latest_activity_at=updated,
                state=_empty_state_json(owner_id, conversation_id),
            )
        )
        normalized = f"{title_prefix} alpha beta document {index:05d}"
        docs.append(
            SearchDocument(
                conversation_id=conversation_id,
                owner_id=owner_id,
                normalized_text=normalized,
                search_title=f"{title_prefix}",
                search_body=f"alpha beta document {index:05d}",
                snippet_text=f"{title_prefix} alpha beta document {index:05d}",
                updated_at=updated,
            )
        )
    ConversationAggregate.objects.bulk_create(rows, batch_size=1000)
    SearchDocument.objects.bulk_create(docs, batch_size=1000)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_list_conversations_p95() -> None:
    await sync_to_async(_seed_shells)(10_000, owner_id=OWNER, title_prefix="list")
    await sync_to_async(_seed_shells)(100, owner_id=OTHER, title_prefix="other")
    persistence = DjangoPersistence()

    def _list() -> Page[ConversationShell]:
        with CaptureQueriesContext(connection) as ctx:
            page = persistence._list_conversations(OWNER, None, 50, True)  # pyright: ignore[reportPrivateUsage]
        assert len(page.items) == 50
        assert len(ctx.captured_queries) <= 2
        return page

    async def operation() -> Page[ConversationShell]:
        return await sync_to_async(_list)()

    p95, _ = await measure_p95_ns(operation)
    assert p95 <= LIST_BUDGET_NS, f"list p95 {p95 / MS:.1f}ms"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_search_conversations_p95() -> None:
    await sync_to_async(_seed_shells)(10_000, owner_id=OWNER, title_prefix="search")
    await sync_to_async(_seed_shells)(100, owner_id=OTHER, title_prefix="search")
    persistence = DjangoPersistence()

    def _search() -> Page[ConversationSearchHit]:
        with CaptureQueriesContext(connection) as ctx:
            page = persistence._search_conversations(OWNER, "alpha beta", None, 50)  # pyright: ignore[reportPrivateUsage]
        assert len(page.items) == 50
        assert len(ctx.captured_queries) <= 3
        return page

    async def operation() -> Page[ConversationSearchHit]:
        return await sync_to_async(_search)()

    p95, _ = await measure_p95_ns(operation)
    assert p95 <= SEARCH_BUDGET_NS, f"search p95 {p95 / MS:.1f}ms"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_idempotent_submit_p95() -> None:
    persistence = DjangoPersistence()
    state = idle_state(OWNER, title="submit-perf")
    await persistence.save_snapshot(state)
    registry = AdapterRegistry()
    broker = InProcessCommittedEventBroker(poll_interval=10.0, keepalive_interval=60.0)
    runtime = RuntimeManager(persistence, registry, clock=lambda: NOW)
    service = TalkToHarnessesService(persistence, registry, broker, lambda: NOW, runtime)
    service._started = True  # pyright: ignore[reportPrivateUsage]
    first = await service.submit_turn(
        OWNER,
        state.conversation.id,
        prompt="first prompt",
        idempotency_key="perf-submit-key",
    )
    assert first.command.id

    async def operation() -> object:
        before = len(connection.queries)
        result = await service.submit_turn(
            OWNER,
            state.conversation.id,
            prompt="first prompt",
            idempotency_key="perf-submit-key",
        )
        # Force sync query log flush via a no-op sync call.
        await sync_to_async(lambda: None)()
        assert result.command.id == first.command.id
        assert len(connection.queries) - before <= 12
        return result

    p95, _ = await measure_p95_ns(operation)
    assert p95 <= SUBMIT_BUDGET_NS, f"submit p95 {p95 / MS:.1f}ms"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_replay_events_p95() -> None:
    persistence = DjangoPersistence()
    state = idle_state(OWNER, title="replay-perf")
    await persistence.save_snapshot(state)
    conversation_id = state.conversation.id

    def _seed_events() -> None:
        events: list[ConversationEventRecord] = []
        total_bytes = 0
        for sequence in range(1, 5001):
            payload = ConversationTitleUpdatedPayload(title_native=f"title-{sequence}")
            event = ConversationEvent(
                conversation_id=conversation_id,
                sequence=sequence,
                timestamp=NOW + timedelta(milliseconds=sequence),
                type=payload.type,
                payload=payload,
            )
            encoded = event.model_dump(mode="json")
            size = len(event.model_dump_json().encode("utf-8"))
            total_bytes += size
            events.append(
                ConversationEventRecord(
                    event_id=event.event_id,
                    conversation_id=conversation_id,
                    sequence=sequence,
                    timestamp=event.timestamp,
                    type=event.type,
                    payload=encoded,
                )
            )
        assert total_bytes <= 5 * 1024 * 1024
        ConversationEventRecord.objects.bulk_create(events, batch_size=1000)

    await sync_to_async(_seed_events)()

    def _replay() -> object:
        with CaptureQueriesContext(connection) as ctx:
            replayed = persistence._replay(  # pyright: ignore[reportPrivateUsage]
                conversation_id,
                0,
                5000,
                5 * 1024 * 1024,
            )
        assert len(replayed) == 5000
        assert len(ctx.captured_queries) <= 2
        return replayed

    async def operation() -> object:
        return await sync_to_async(_replay)()

    p95, _ = await measure_p95_ns(operation)
    assert p95 <= REPLAY_BUDGET_NS, f"replay p95 {p95 / MS:.1f}ms"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_sse_delivery_p95() -> None:
    conversation_id = uuid4()
    broker = InProcessCommittedEventBroker(poll_interval=10.0, keepalive_interval=60.0)
    await broker.start()
    budget = SSE_POSTGRES_BUDGET_NS if connection.vendor == "postgresql" else SSE_SQLITE_BUDGET_NS
    try:

        async def operation() -> int:
            agen = broker.subscribe(conversation_id).__aiter__()

            async def _next() -> ConversationWakeup:
                return await agen.__anext__()

            consumer = asyncio.create_task(_next())
            await asyncio.sleep(0)
            payload = ConversationTitleUpdatedPayload(title_native="sse")
            event = ConversationEvent(
                conversation_id=conversation_id,
                sequence=1,
                timestamp=datetime.now(tz=UTC),
                type=payload.type,
                payload=payload,
            )
            started = time.perf_counter_ns()
            await broker.publish((event,))
            wakeup = await asyncio.wait_for(consumer, timeout=2.0)
            elapsed = time.perf_counter_ns() - started
            assert wakeup.sequence >= 1
            return elapsed

        samples: list[int] = []
        for _ in range(5):
            await operation()
        for _ in range(30):
            samples.append(await operation())
        p95 = percentile_ns(samples, 95)
        assert p95 <= budget, f"sse p95 {p95 / MS:.1f}ms vendor={connection.vendor}"
    finally:
        await broker.stop()
