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
    CommandKind,
    CommandStatus,
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessKind,
    append_events,
    apply_steer,
    complete_turn,
    new_conversation_state,
    start_turn,
    submit_turn,
)
from talktoharnesses.domain.events import (
    ConversationEvent,
    HarnessEvent,
    ProviderWarningPayload,
    TurnCompletedPayload,
)
from talktoharnesses.domain.models import ConversationHarnessBinding, EditQueuedPayload
from talktoharnesses.providers.adapter import HarnessSession, SteerRequest, TurnRequest


class _Publisher:
    def __init__(self) -> None:
        self.events: list[ConversationEvent] = []

    async def publish(self, events: Sequence[ConversationEvent]) -> None:
        self.events.extend(events)


class _Adapter:
    def __init__(self, *, steer_ok: bool = True) -> None:
        self.submissions: list[TurnRequest] = []
        self.steers: list[SteerRequest] = []
        self.steer_ok = steer_ok
        self.imported: tuple[frozenset[str], frozenset[str]] | None = None

    async def submit(self, session: HarnessSession, request: TurnRequest) -> None:
        self.submissions.append(request)

    async def steer(self, session: HarnessSession, request: SteerRequest) -> bool:
        self.steers.append(request)
        return self.steer_ok

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


def _bound_state(*, steer: bool = False):
    now = datetime(2026, 8, 8, tzinfo=UTC)
    state = new_conversation_state(
        owner_id="owner",
        now=now,
        capabilities=HarnessCapabilities(
            kind=HarnessKind.GROK,
            version="1.0.0",
            supports_steer=steer,
            supports_interrupt=True,
            supports_resume=True,
        ),
    )
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
    return now, state


@pytest.mark.asyncio
async def test_queued_submit_does_not_run_against_active_turn() -> None:
    now, state = _bound_state()
    first = submit_turn(state, prompt="active", idempotency_key="a", now=now)
    started = start_turn(first.state, now=now)
    second = submit_turn(started.state, prompt="queued", idempotency_key="b", now=now)
    assert second.command is not None

    persistence = MemoryPersistence()
    persistence.seed(second.state)
    await persistence.accept_command(first.command)  # type: ignore[arg-type]
    await persistence.accept_command(second.command)
    claimed = second.command.model_copy(
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
    assert state.binding is not None
    runtime.managed = SimpleNamespace(
        adapter=adapter,
        session=HarnessSession(
            conversation_id=state.conversation.id,
            binding_id=state.binding.id,
            kind=HarnessKind.GROK,
            native_session_id="session-1",
        ),
    )
    processor = CommandProcessor(persistence, _Publisher(), runtime)  # type: ignore[arg-type]
    processor._worker_id = "worker-1"  # pyright: ignore[reportPrivateUsage]

    await processor._execute_command(claimed)  # pyright: ignore[reportPrivateUsage]

    assert adapter.submissions == []
    stored = persistence.commands[claimed.id]
    assert stored.status is CommandStatus.CLAIMED
    assert stored.delivery_started_at is None
    aggregate = await persistence.get_worker_snapshot(state.conversation.id)
    assert aggregate.commands[claimed.id].status is CommandStatus.CLAIMED

    terminal = complete_turn(aggregate, now=now, has_assistant_message=False)
    await persistence.commit_turn_batch(
        state.conversation.id,
        aggregate.conversation.version,
        terminal.state,
        terminal.events,
        tuple(terminal.state.commands.values()),
    )
    await processor._wake_queued_submit(  # pyright: ignore[reportPrivateUsage]
        state.conversation.id
    )
    assert persistence.commands[claimed.id].status is CommandStatus.ACCEPTED
    assert claimed.id in persistence.accepted_queue
    aggregate = await persistence.get_worker_snapshot(state.conversation.id)
    assert aggregate.commands[claimed.id].status is CommandStatus.ACCEPTED


@pytest.mark.asyncio
async def test_unsupported_command_is_settled() -> None:
    now, state = _bound_state()
    persistence = MemoryPersistence()
    persistence.seed(state)
    from talktoharnesses.domain.models import Command

    command = Command(
        conversation_id=state.conversation.id,
        kind=CommandKind.EDIT_QUEUED,
        status=CommandStatus.CLAIMED,
        idempotency_key="edit-1",
        payload=EditQueuedPayload(prompt="x"),
        created_at=now,
        worker_id="worker-1",
        attempts=1,
        lease_expires_at=now + timedelta(seconds=30),
    )
    persistence.commands[command.id] = command
    adapter = _Adapter()
    runtime = _Runtime(persistence, adapter)
    assert state.binding is not None
    runtime.managed = SimpleNamespace(
        adapter=adapter,
        session=HarnessSession(
            conversation_id=state.conversation.id,
            binding_id=state.binding.id,
            kind=HarnessKind.GROK,
            native_session_id="session-1",
        ),
    )
    processor = CommandProcessor(persistence, _Publisher(), runtime)  # type: ignore[arg-type]
    processor._worker_id = "worker-1"  # pyright: ignore[reportPrivateUsage]

    await processor._execute_command(command)  # pyright: ignore[reportPrivateUsage]

    stored = persistence.commands[command.id]
    assert stored.status is CommandStatus.SETTLED
    assert stored.settled_at is not None


@pytest.mark.asyncio
async def test_steer_failure_queues_instead_of_delivered() -> None:
    now, state = _bound_state(steer=True)
    first = submit_turn(state, prompt="active", idempotency_key="a", now=now)
    started = start_turn(first.state, now=now)
    steered = apply_steer(
        started.state,
        prompt="nudge",
        idempotency_key="s1",
        now=now,
        steer_succeeded=True,
    )
    assert steered.command is not None

    persistence = MemoryPersistence()
    persistence.seed(steered.state)
    await persistence.accept_command(steered.command)
    claimed = steered.command.model_copy(
        update={
            "status": CommandStatus.CLAIMED,
            "worker_id": "worker-1",
            "attempts": 1,
            "lease_expires_at": now + timedelta(seconds=30),
        }
    )
    persistence.commands[claimed.id] = claimed
    adapter = _Adapter(steer_ok=False)
    runtime = _Runtime(persistence, adapter)
    assert state.binding is not None
    runtime.managed = SimpleNamespace(
        adapter=adapter,
        session=HarnessSession(
            conversation_id=state.conversation.id,
            binding_id=state.binding.id,
            kind=HarnessKind.GROK,
            native_session_id="session-1",
        ),
    )
    processor = CommandProcessor(persistence, _Publisher(), runtime)  # type: ignore[arg-type]
    processor._worker_id = "worker-1"  # pyright: ignore[reportPrivateUsage]

    await processor._execute_command(claimed)  # pyright: ignore[reportPrivateUsage]

    assert len(adapter.steers) == 1
    snap = await persistence.get_worker_snapshot(state.conversation.id)
    assert snap.queued_user_text == "nudge"
    stored = persistence.commands[claimed.id]
    assert stored.status is CommandStatus.ACCEPTED
    assert stored.kind is CommandKind.SUBMIT_TURN
    assert stored.delivered_at is None
