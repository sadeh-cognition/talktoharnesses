"""End-to-end command worker regressions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from tests.runtime.memory_persistence import MemoryPersistence

from talktoharnesses.application.command_processor import CommandProcessor
from talktoharnesses.domain import (
    CommandStatus,
    HarnessConfiguration,
    HarnessKind,
    append_events,
    new_conversation_state,
    submit_turn,
)
from talktoharnesses.domain.events import (
    ConversationEvent,
    HarnessEvent,
    ProviderWarningPayload,
    TurnCompletedPayload,
)
from talktoharnesses.domain.models import ConversationHarnessBinding
from talktoharnesses.providers.adapter import HarnessSession, TurnRequest


class _Publisher:
    def __init__(self) -> None:
        self.events: list[ConversationEvent] = []

    async def publish(self, events: Sequence[ConversationEvent]) -> None:
        self.events.extend(events)


class _Adapter:
    def __init__(self) -> None:
        self.submissions: list[TurnRequest] = []
        self.imported: tuple[frozenset[str], frozenset[str]] | None = None

    async def submit(self, session: HarnessSession, request: TurnRequest) -> None:
        self.submissions.append(request)

    def import_seen(
        self,
        native_ids: Iterable[str],
        stream_offsets: Iterable[str],
    ) -> None:
        self.imported = frozenset(native_ids), frozenset(stream_offsets)

    def export_seen(self) -> tuple[frozenset[str], frozenset[str]]:
        return frozenset({"native-1"}), frozenset({"session-1:1"})

    def events(self, session: HarnessSession) -> AsyncIterator[HarnessEvent]:
        async def gen() -> AsyncIterator[HarnessEvent]:
            while not self.submissions:
                await asyncio.sleep(0)
            yield TurnCompletedPayload(
                turn_id=self.submissions[0].turn_id,
                terminal_reason="end_turn",
            )

        return gen()


class _Runtime:
    def __init__(self, persistence: MemoryPersistence, adapter: _Adapter) -> None:
        self.persistence = persistence
        self.adapter = adapter
        self.managed = None

    def get_runtime(self, conversation_id: UUID):
        return self.managed

    async def start(
        self,
        *,
        conversation_id: UUID,
        owner_id: str,
        **kwargs: Any,
    ) -> HarnessSession:
        # Model the lifecycle commit performed by RuntimeManager.start(). This
        # invalidates the snapshot the command worker loaded before lazy start.
        state = await self.persistence.get_worker_snapshot(conversation_id)
        next_state, events = append_events(
            state,
            datetime.now(UTC),
            [ProviderWarningPayload(message="runtime started")],
        )
        await self.persistence.commit_runtime_lifecycle(
            conversation_id,
            state.conversation.version,
            next_state,
            None,
            None,
            events,
        )
        assert state.binding is not None
        session = HarnessSession(
            conversation_id=conversation_id,
            binding_id=state.binding.id,
            kind=HarnessKind.GROK,
            native_session_id="session-1",
        )
        self.managed = SimpleNamespace(adapter=self.adapter, session=session)
        return session

    async def resume(self, **kwargs: Any) -> HarnessSession:
        return await self.start(**kwargs)

    async def close(self, conversation_id: UUID, *, reason: str) -> None:
        self.managed = None


class _HangingRuntime:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    def get_runtime(self, conversation_id: UUID):
        return None

    async def start(self, **kwargs: Any) -> None:
        self.started.set()
        try:
            await asyncio.Future()
        finally:
            self.cancelled.set()

    async def resume(self, **kwargs: Any) -> None:
        return await self.start(**kwargs)


@pytest.mark.asyncio
async def test_lazy_start_delivers_coalesced_prompt_with_claim_and_dedupe_state() -> None:
    now = datetime(2026, 8, 8, tzinfo=UTC)
    state = new_conversation_state(owner_id="owner", now=now)
    binding = ConversationHarnessBinding(
        conversation_id=state.conversation.id,
        kind=HarnessKind.GROK,
        configuration=HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp"),
        created_at=now,
    )
    state = state.model_copy(
        update={
            "binding": binding,
            "conversation": state.conversation.model_copy(
                update={"current_binding_id": binding.id}
            ),
        }
    )
    first = submit_turn(state, prompt="one", idempotency_key="one", now=now)
    assert first.command is not None
    second = submit_turn(first.state, prompt="two", idempotency_key="two", now=now)
    assert second.command is not None

    persistence = MemoryPersistence()
    persistence.seed(second.state)
    await persistence.accept_command(first.command)
    await persistence.accept_command(second.command)
    claimed = first.command.model_copy(
        update={
            "status": CommandStatus.CLAIMED,
            "worker_id": "worker-1",
            "attempts": 1,
            "lease_expires_at": now + timedelta(seconds=30),
        }
    )
    persistence.commands[claimed.id] = claimed
    adapter = _Adapter()
    runtime = _Runtime(persistence, adapter)
    processor = CommandProcessor(persistence, _Publisher(), runtime)  # type: ignore[arg-type]
    processor._worker_id = "worker-1"  # pyright: ignore[reportPrivateUsage]

    await processor._execute_command(claimed)  # pyright: ignore[reportPrivateUsage]
    final = await persistence.get_worker_snapshot(state.conversation.id)
    for _ in range(100):
        final = await persistence.get_worker_snapshot(state.conversation.id)
        if final.active_turn is None:
            break
        await asyncio.sleep(0.01)
    await processor.stop()

    assert adapter.submissions[0].prompt == "one\ntwo"
    stored = persistence.commands[claimed.id]
    assert stored.worker_id == "worker-1"
    assert stored.attempts == 1
    assert final.seen_native_ids == frozenset({"native-1"})
    assert final.seen_stream_offsets == frozenset({"session-1:1"})


@pytest.mark.asyncio
async def test_stop_waits_for_in_flight_command_tasks() -> None:
    now = datetime(2026, 8, 8, tzinfo=UTC)
    state = new_conversation_state(owner_id="owner", now=now)
    binding = ConversationHarnessBinding(
        conversation_id=state.conversation.id,
        kind=HarnessKind.GROK,
        configuration=HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp"),
        created_at=now,
    )
    state = state.model_copy(update={"binding": binding})
    queued = submit_turn(state, prompt="wait", idempotency_key="wait", now=now)
    assert queued.command is not None
    persistence = MemoryPersistence()
    persistence.seed(queued.state)
    await persistence.accept_command(queued.command)
    runtime = _HangingRuntime()
    processor = CommandProcessor(
        persistence,
        _Publisher(),
        runtime,  # type: ignore[arg-type]
        poll_interval=0.001,
    )

    await processor.start("worker")
    await asyncio.wait_for(runtime.started.wait(), timeout=1)
    await processor.stop()

    assert runtime.cancelled.is_set()
    assert not processor._command_tasks  # pyright: ignore[reportPrivateUsage]
