"""End-to-end command worker regressions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.runtime.memory_persistence import MemoryPersistence

from talktoharnesses.application.command_processor import (
    MAX_TRANSIENT_STARTUP_ATTEMPTS,
    TRANSIENT_STARTUP_ERRORS,
    CommandProcessor,
)
from talktoharnesses.domain import (
    CommandKind,
    CommandStatus,
    ErrorCode,
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
from talktoharnesses.domain.enums import TurnStatus
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import (
    AssistantMessageDeltaPayload,
    ConversationEvent,
    HarnessEvent,
    ProviderWarningPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
)
from talktoharnesses.domain.models import (
    Command,
    ConversationHarnessBinding,
    EditQueuedPayload,
    InterruptPayload,
)
from talktoharnesses.providers.adapter import HarnessSession, SteerRequest, TurnRequest

_TRANSIENT_CODES: list[ErrorCode] = sorted(TRANSIENT_STARTUP_ERRORS, key=lambda c: c.value)


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


class _ConflictingAdapter(_Adapter):
    def __init__(
        self,
        persistence: MemoryPersistence,
        conversation_id: UUID,
        turn_id: UUID,
        *,
        invalidate_runtime: bool = False,
    ) -> None:
        super().__init__()
        self._persistence = persistence
        self._conversation_id = conversation_id
        self._turn_id = turn_id
        self._invalidate_runtime = invalidate_runtime

    def events(self, session: HarnessSession) -> AsyncIterator[HarnessEvent]:
        async def gen() -> AsyncIterator[HarnessEvent]:
            yield AssistantMessageDeltaPayload(
                turn_id=self._turn_id,
                message_id=uuid4(),
                sequence=1,
                text="partial",
            )
            state = await self._persistence.get_worker_snapshot(self._conversation_id)
            if self._invalidate_runtime:
                assert state.binding is not None
                state = state.model_copy(
                    update={
                        "binding": state.binding.model_copy(
                            update={"requires_session_recreation": True}
                        )
                    }
                )
            next_state, events = append_events(
                state,
                datetime.now(UTC),
                [ProviderWarningPayload(message="concurrent lifecycle commit")],
            )
            await self._persistence.commit_runtime_lifecycle(
                self._conversation_id,
                state.conversation.version,
                next_state,
                None,
                None,
                events,
            )
            await asyncio.sleep(0.1)

        return gen()


class _Runtime:
    def __init__(self, persistence: MemoryPersistence, adapter: _Adapter) -> None:
        self.persistence = persistence
        self.adapter = adapter
        self.managed = None
        self.closed_replaced_reason: str | None = None

    def get_runtime(self, conversation_id: UUID):
        return self.managed

    async def ensure_binding_current(self, conversation_id: UUID, state: Any):
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
        assert state.binding is not None
        binding = state.binding.model_copy(update={"native_session_id": "session-1"})
        state = state.model_copy(update={"binding": binding})
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
        session = HarnessSession(
            conversation_id=conversation_id,
            binding_id=binding.id,
            kind=HarnessKind.GROK,
            native_session_id="session-1",
        )
        self.managed = SimpleNamespace(adapter=self.adapter, session=session)
        return session

    async def resume(self, **kwargs: Any) -> HarnessSession:
        return await self.start(**kwargs)

    async def close(self, conversation_id: UUID, *, reason: str) -> None:
        self.managed = None

    async def close_replaced_runtime(self, managed: Any, *, reason: str) -> None:
        self.closed_replaced_reason = reason
        if self.managed is managed:
            self.managed = None


class _HangingRuntime:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    def get_runtime(self, conversation_id: UUID):
        return None

    async def ensure_binding_current(self, conversation_id: UUID, state: Any):
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
async def test_event_batch_rebases_after_concurrent_lifecycle_commit() -> None:
    now, state = _bound_state()
    queued = submit_turn(state, prompt="active", idempotency_key="active", now=now)
    started = start_turn(queued.state, now=now)
    assert started.state.active_turn is not None

    persistence = MemoryPersistence()
    persistence.seed(started.state)
    adapter = _ConflictingAdapter(
        persistence,
        state.conversation.id,
        started.state.active_turn.id,
    )
    runtime = _Runtime(persistence, adapter)
    assert state.binding is not None
    runtime.managed = SimpleNamespace(
        adapter=adapter,
        session=HarnessSession(
            conversation_id=state.conversation.id,
            binding_id=state.binding.id,
            kind=HarnessKind.GROK,
        ),
    )
    publisher = _Publisher()
    processor = CommandProcessor(persistence, publisher, runtime)  # type: ignore[arg-type]

    await processor._event_pump(state.conversation.id)  # pyright: ignore[reportPrivateUsage]

    event_types = [event.type for event in persistence.events[state.conversation.id]]
    assert event_types == ["provider_warning", "assistant_message_delta"]
    assert [event.type for event in publisher.events] == ["assistant_message_delta"]


@pytest.mark.asyncio
async def test_event_batch_discards_stale_runtime_after_rotation_conflict() -> None:
    now, state = _bound_state()
    queued = submit_turn(state, prompt="active", idempotency_key="active", now=now)
    started = start_turn(queued.state, now=now)
    assert started.state.active_turn is not None

    persistence = MemoryPersistence()
    persistence.seed(started.state)
    adapter = _ConflictingAdapter(
        persistence,
        state.conversation.id,
        started.state.active_turn.id,
        invalidate_runtime=True,
    )
    runtime = _Runtime(persistence, adapter)
    assert state.binding is not None
    runtime.managed = SimpleNamespace(
        adapter=adapter,
        session=HarnessSession(
            conversation_id=state.conversation.id,
            binding_id=state.binding.id,
            kind=HarnessKind.GROK,
        ),
    )
    publisher = _Publisher()
    processor = CommandProcessor(persistence, publisher, runtime)  # type: ignore[arg-type]

    await processor._event_pump(state.conversation.id)  # pyright: ignore[reportPrivateUsage]

    assert [event.type for event in persistence.events[state.conversation.id]] == [
        "provider_warning"
    ]
    assert publisher.events == []
    assert runtime.closed_replaced_reason == "stale_binding"


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


class _SlowStartRuntime(_Runtime):
    """Models a cold sandbox start that outlives the claim lease."""

    def __init__(self, persistence: MemoryPersistence, adapter: _Adapter, delay: float) -> None:
        super().__init__(persistence, adapter)
        self.delay = delay
        self.starts = 0

    async def ensure_binding_current(self, conversation_id: UUID, state: Any):
        return self.managed

    async def start(self, **kwargs: Any) -> HarnessSession:
        self.starts += 1
        await asyncio.sleep(self.delay)
        return await super().start(**kwargs)


def _seed_submit(prompt: str = "one") -> tuple[datetime, Any, Any, MemoryPersistence]:
    now, state = _bound_state()
    queued = submit_turn(state, prompt=prompt, idempotency_key=prompt, now=now)
    assert queued.command is not None
    persistence = MemoryPersistence()
    persistence.seed(queued.state)
    return now, state, queued.command, persistence


async def _wait_for_settled(persistence: MemoryPersistence, command_id: UUID) -> None:
    for _ in range(300):
        stored = persistence.commands[command_id]
        if stored.status is CommandStatus.SETTLED:
            return
        await asyncio.sleep(0.01)


def _refresh_ownership(persistence: MemoryPersistence, conversation_id: UUID) -> asyncio.Task[None]:
    """Model the worker coordinator heartbeat, which keeps this worker's
    conversation ownership lease fresh independently of command delivery."""

    async def refresh() -> None:
        while True:
            ownership = persistence.ownership.get(conversation_id)
            if ownership is not None:
                persistence.ownership[conversation_id] = (
                    ownership[0],
                    ownership[1],
                    datetime.now(UTC) + timedelta(seconds=30),
                )
            await asyncio.sleep(0.02)

    return asyncio.create_task(refresh())


@pytest.mark.asyncio
async def test_expired_lease_during_cold_start_does_not_double_deliver() -> None:
    """Regression for the duplicate-turn incident: a cold start longer than
    the claim lease must not lead to a second adapter.submit."""
    _, state, command, persistence = _seed_submit()
    await persistence.accept_command(command)
    adapter = _Adapter()
    runtime = _SlowStartRuntime(persistence, adapter, delay=0.6)
    processor = CommandProcessor(
        persistence,
        _Publisher(),
        runtime,  # type: ignore[arg-type]
        lease_seconds=0.2,
        poll_interval=0.02,
    )

    heartbeat = _refresh_ownership(persistence, state.conversation.id)
    await processor.start("worker-1")
    try:
        await _wait_for_settled(persistence, command.id)
        # Leave room for any duplicate task to run before shutdown.
        await asyncio.sleep(0.1)
    finally:
        await processor.stop()
        heartbeat.cancel()

    assert len(adapter.submissions) == 1
    stored = persistence.commands[command.id]
    assert stored.status is CommandStatus.SETTLED
    # The keepalive kept the lease fresh, so the claim loop never re-claimed.
    assert stored.attempts == 1


@pytest.mark.asyncio
async def test_reclaimed_in_flight_command_is_not_delivered_twice() -> None:
    """Even without the lease keepalive, a re-claim of our own in-flight
    command must be skipped by the claim loop."""
    _, state, command, persistence = _seed_submit()
    await persistence.accept_command(command)
    adapter = _Adapter()
    runtime = _SlowStartRuntime(persistence, adapter, delay=0.6)
    processor = CommandProcessor(
        persistence,
        _Publisher(),
        runtime,  # type: ignore[arg-type]
        lease_seconds=0.2,
        poll_interval=0.02,
    )
    processor._spawn_lease_keepalive = lambda command: None  # type: ignore[method-assign] # pyright: ignore[reportPrivateUsage]

    heartbeat = _refresh_ownership(persistence, state.conversation.id)
    await processor.start("worker-1")
    try:
        await _wait_for_settled(persistence, command.id)
        await asyncio.sleep(0.1)
    finally:
        await processor.stop()
        heartbeat.cancel()

    assert len(adapter.submissions) == 1
    assert runtime.starts == 1
    stored = persistence.commands[command.id]
    assert stored.status is CommandStatus.SETTLED
    # The lease expired mid-start, so the claim loop re-claimed at least once
    # and skipped spawning a duplicate task each time.
    assert stored.attempts > 1


@pytest.mark.asyncio
async def test_lease_keepalive_renews_during_slow_start() -> None:
    _, _, command, persistence = _seed_submit()
    await persistence.accept_command(command)
    claimed = command.model_copy(
        update={
            "status": CommandStatus.CLAIMED,
            "worker_id": "worker-1",
            "attempts": 1,
            "lease_expires_at": datetime.now(UTC) + timedelta(seconds=0.15),
        }
    )
    persistence.commands[claimed.id] = claimed
    initial_lease = claimed.lease_expires_at
    adapter = _Adapter()
    runtime = _SlowStartRuntime(persistence, adapter, delay=0.4)
    processor = CommandProcessor(
        persistence,
        _Publisher(),
        runtime,  # type: ignore[arg-type]
        lease_seconds=0.15,
    )
    processor._worker_id = "worker-1"  # pyright: ignore[reportPrivateUsage]

    await processor._execute_command(claimed)  # pyright: ignore[reportPrivateUsage]
    await processor.stop()

    stored = persistence.commands[claimed.id]
    assert initial_lease is not None
    assert stored.lease_expires_at is not None
    assert stored.lease_expires_at > initial_lease
    assert stored.attempts == 1
    assert len(adapter.submissions) == 1


def _guard_fixture(
    *,
    status: CommandStatus,
    delivery_started: bool,
    delivered: bool = False,
) -> tuple[Any, MemoryPersistence, _Adapter, CommandProcessor, Any]:
    now, state = _bound_state()
    queued = submit_turn(state, prompt="guarded", idempotency_key="guarded", now=now)
    assert queued.command is not None
    started = start_turn(queued.state, now=now)
    persistence = MemoryPersistence()
    persistence.seed(started.state)
    durable = queued.command.model_copy(
        update={
            "status": status,
            "worker_id": "worker-1",
            "attempts": 1,
            "lease_expires_at": now + timedelta(seconds=30),
            "delivery_started_at": now if delivery_started else None,
            "delivered_at": now if delivered else None,
        }
    )
    persistence.commands[durable.id] = durable
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
    claimed = durable.model_copy(
        update={"status": CommandStatus.CLAIMED, "delivery_started_at": None}
    )
    return claimed, persistence, adapter, processor, durable


@pytest.mark.asyncio
async def test_delivered_command_is_not_redelivered() -> None:
    claimed, persistence, adapter, processor, _ = _guard_fixture(
        status=CommandStatus.DELIVERED,
        delivery_started=True,
        delivered=True,
    )

    await processor._execute_command(claimed)  # pyright: ignore[reportPrivateUsage]
    await processor.stop()

    assert adapter.submissions == []
    assert persistence.commands[claimed.id].status is CommandStatus.DELIVERED


@pytest.mark.asyncio
async def test_ambiguous_prior_delivery_marks_outcome_unknown() -> None:
    claimed, persistence, adapter, processor, _ = _guard_fixture(
        status=CommandStatus.CLAIMED,
        delivery_started=True,
    )

    await processor._execute_command(claimed)  # pyright: ignore[reportPrivateUsage]
    await processor.stop()

    assert adapter.submissions == []
    stored = persistence.commands[claimed.id]
    assert stored.status is CommandStatus.OUTCOME_UNKNOWN
    assert stored.worker_id is None
    assert stored.lease_expires_at is None


@pytest.mark.asyncio
async def test_settled_command_is_skipped() -> None:
    claimed, persistence, adapter, processor, _ = _guard_fixture(
        status=CommandStatus.SETTLED,
        delivery_started=True,
        delivered=True,
    )

    await processor._execute_command(claimed)  # pyright: ignore[reportPrivateUsage]
    await processor.stop()

    assert adapter.submissions == []
    assert persistence.commands[claimed.id].status is CommandStatus.SETTLED


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        DomainError(ErrorCode.SANDBOX_PATH_NOT_MOUNTED, "not mounted"),
        DomainError(ErrorCode.SANDBOX_UNAVAILABLE, "unavailable"),
        DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "private provider output",
            details={"reason": "authentication_failed"},
        ),
        DomainError(ErrorCode.PROTOCOL_ERROR, "split HTTP 500"),
        DomainError(ErrorCode.RUNTIME_TIMEOUT, "startup timed out"),
        RuntimeError("unexpected startup failure with private output"),
    ],
)
@pytest.mark.parametrize("active", [False, True])
async def test_startup_error_settles_command_instead_of_retrying(
    error: Exception,
    active: bool,
) -> None:
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
    submitted = submit_turn(state, prompt="hello", idempotency_key="k1", now=now)
    assert submitted.command is not None

    persistence = MemoryPersistence()
    persistence.seed(start_turn(submitted.state, now=now).state if active else submitted.state)
    await persistence.accept_command(submitted.command)
    # Transient codes get a bounded number of retries; at the cap they settle.
    attempts = (
        MAX_TRANSIENT_STARTUP_ATTEMPTS
        if isinstance(error, DomainError) and error.code in TRANSIENT_STARTUP_ERRORS
        else 1
    )
    claimed = submitted.command.model_copy(
        update={
            "status": CommandStatus.CLAIMED,
            "worker_id": "worker-1",
            "attempts": attempts,
            "lease_expires_at": now + timedelta(seconds=30),
        }
    )
    persistence.commands[claimed.id] = claimed

    class _SandboxlessRuntime:
        def get_runtime(self, conversation_id: UUID):
            return None

        async def ensure_binding_current(self, conversation_id: UUID, state: Any):
            return None

        async def start(self, **kwargs: Any) -> None:
            raise error

        async def resume(self, **kwargs: Any) -> None:
            await self.start(**kwargs)

        async def close(self, conversation_id: UUID, *, reason: str) -> None:
            return None

    publisher = _Publisher()
    processor = CommandProcessor(persistence, publisher, _SandboxlessRuntime())  # type: ignore[arg-type]
    processor._worker_id = "worker-1"  # pyright: ignore[reportPrivateUsage]

    await processor._handle_command(claimed)  # pyright: ignore[reportPrivateUsage]
    await processor.stop()

    stored = persistence.commands[claimed.id]
    assert stored.status is CommandStatus.SETTLED
    assert stored.worker_id is None
    assert stored.lease_expires_at is None
    final = await persistence.get_worker_snapshot(state.conversation.id)
    assert final.queued_turn is None
    assert final.active_turn is None
    failures = [
        event.payload for event in publisher.events if isinstance(event.payload, TurnFailedPayload)
    ]
    assert len(failures) == 1
    assert failures[0].turn_id == submitted.command.target_turn_id
    expected_code = error.code if isinstance(error, DomainError) else ErrorCode.INVALID_STATE
    assert failures[0].error_code == expected_code.value
    assert "private output" not in failures[0].message
    if expected_code is ErrorCode.PROVIDER_INCOMPATIBLE:
        assert "authentication failed" in failures[0].message
    await processor._handle_command(claimed)  # pyright: ignore[reportPrivateUsage]
    assert len(publisher.events) == 1


@pytest.mark.asyncio
async def test_sandbox_preparing_keeps_command_claimed_for_retry() -> None:
    """Transient SANDBOX_PREPARING must keep the lease-retry behavior."""
    from talktoharnesses.domain.enums import ErrorCode
    from talktoharnesses.domain.errors import DomainError

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
    submitted = submit_turn(state, prompt="hello", idempotency_key="k1", now=now)
    assert submitted.command is not None

    persistence = MemoryPersistence()
    persistence.seed(submitted.state)
    await persistence.accept_command(submitted.command)
    claimed = submitted.command.model_copy(
        update={
            "status": CommandStatus.CLAIMED,
            "worker_id": "worker-1",
            "attempts": 1,
            "lease_expires_at": now + timedelta(seconds=30),
        }
    )
    persistence.commands[claimed.id] = claimed

    class _PreparingRuntime:
        def get_runtime(self, conversation_id: UUID):
            return None

        async def ensure_binding_current(self, conversation_id: UUID, state: Any):
            return None

        async def start(self, **kwargs: Any) -> None:
            raise DomainError(
                ErrorCode.SANDBOX_PREPARING,
                "sandbox for grok is being prepared",
                details={"kind": "grok"},
            )

        async def resume(self, **kwargs: Any) -> None:
            await self.start(**kwargs)

        async def close(self, conversation_id: UUID, *, reason: str) -> None:
            return None

    publisher = _Publisher()
    processor = CommandProcessor(persistence, publisher, _PreparingRuntime())  # type: ignore[arg-type]
    processor._worker_id = "worker-1"  # pyright: ignore[reportPrivateUsage]

    await processor._handle_command(claimed)  # pyright: ignore[reportPrivateUsage]
    await processor.stop()

    stored = persistence.commands[claimed.id]
    assert stored.status is CommandStatus.CLAIMED
    final = await persistence.get_worker_snapshot(state.conversation.id)
    assert final.queued_turn is not None


class _FailingStartRuntime:
    def __init__(self, error: BaseException, *, delay: float = 0.0) -> None:
        self.error = error
        self.delay = delay
        self.starts = 0

    def get_runtime(self, conversation_id: UUID):
        return None

    async def ensure_binding_current(self, conversation_id: UUID, state: Any):
        return None

    async def start(self, **kwargs: Any) -> None:
        self.starts += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        raise self.error

    async def resume(self, **kwargs: Any) -> None:
        await self.start(**kwargs)

    async def close(self, conversation_id: UUID, *, reason: str) -> None:
        return None


def _claimed(command: Command, now: datetime, *, attempts: int = 1) -> Command:
    return command.model_copy(
        update={
            "status": CommandStatus.CLAIMED,
            "worker_id": "worker-1",
            "attempts": attempts,
            "lease_expires_at": now + timedelta(seconds=30),
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("code", _TRANSIENT_CODES)
async def test_transient_startup_error_keeps_command_claimed_below_attempt_cap(
    code: ErrorCode,
) -> None:
    """A split restarting mid-probe must be retried, not fail the user's turn."""
    now, state = _bound_state()
    submitted = submit_turn(state, prompt="hello", idempotency_key="k1", now=now)
    assert submitted.command is not None
    persistence = MemoryPersistence()
    persistence.seed(submitted.state)
    await persistence.accept_command(submitted.command)
    claimed = _claimed(submitted.command, now, attempts=MAX_TRANSIENT_STARTUP_ATTEMPTS - 1)
    persistence.commands[claimed.id] = claimed

    publisher = _Publisher()
    runtime = _FailingStartRuntime(DomainError(code, "split request failed"))
    processor = CommandProcessor(persistence, publisher, runtime)  # type: ignore[arg-type]
    processor._worker_id = "worker-1"  # pyright: ignore[reportPrivateUsage]

    await processor._handle_command(claimed)  # pyright: ignore[reportPrivateUsage]
    await processor.stop()

    assert persistence.commands[claimed.id].status is CommandStatus.CLAIMED
    assert publisher.events == []
    final = await persistence.get_worker_snapshot(state.conversation.id)
    assert final.queued_turn is not None


@pytest.mark.asyncio
async def test_startup_failure_fails_active_turn_for_non_turn_command() -> None:
    """INTERRUPT against a runtime that can never start must still terminalize
    the active turn instead of settling silently and leaving it RUNNING."""
    now, state = _bound_state()
    submitted = submit_turn(state, prompt="hello", idempotency_key="k1", now=now)
    assert submitted.command is not None
    started = start_turn(submitted.state, now=now)
    assert started.state.active_turn is not None
    interrupt = Command(
        conversation_id=state.conversation.id,
        kind=CommandKind.INTERRUPT,
        status=CommandStatus.ACCEPTED,
        idempotency_key="interrupt-1",
        payload=InterruptPayload(),
        created_at=now,
    )
    persistence = MemoryPersistence()
    persistence.seed(started.state)
    await persistence.accept_command(interrupt)
    claimed = _claimed(interrupt, now)
    persistence.commands[claimed.id] = claimed

    publisher = _Publisher()
    runtime = _FailingStartRuntime(DomainError(ErrorCode.SANDBOX_UNAVAILABLE, "gone"))
    processor = CommandProcessor(persistence, publisher, runtime)  # type: ignore[arg-type]
    processor._worker_id = "worker-1"  # pyright: ignore[reportPrivateUsage]

    await processor._handle_command(claimed)  # pyright: ignore[reportPrivateUsage]
    await processor.stop()

    assert persistence.commands[claimed.id].status is CommandStatus.SETTLED
    final = await persistence.get_worker_snapshot(state.conversation.id)
    assert final.active_turn is None
    failures = [
        event.payload for event in publisher.events if isinstance(event.payload, TurnFailedPayload)
    ]
    assert [failure.turn_id for failure in failures] == [started.state.active_turn.id]
    assert failures[0].error_code == ErrorCode.SANDBOX_UNAVAILABLE.value


@pytest.mark.asyncio
async def test_startup_failure_fails_waiting_active_turn_and_queued_turn() -> None:
    """A queued SUBMIT hitting a permanent startup failure must not leave the
    conversation WAITING with no runtime and no command left to drive it."""
    now, state = _bound_state()
    first = submit_turn(state, prompt="active", idempotency_key="a", now=now)
    started = start_turn(first.state, now=now)
    assert started.state.active_turn is not None
    waiting = started.state.model_copy(
        update={
            "active_turn": started.state.active_turn.model_copy(
                update={"status": TurnStatus.WAITING}
            )
        }
    )
    second = submit_turn(waiting, prompt="queued", idempotency_key="b", now=now)
    assert second.command is not None
    assert second.state.queued_turn is not None
    persistence = MemoryPersistence()
    persistence.seed(second.state)
    await persistence.accept_command(second.command)
    claimed = _claimed(second.command, now)
    persistence.commands[claimed.id] = claimed

    publisher = _Publisher()
    runtime = _FailingStartRuntime(DomainError(ErrorCode.SANDBOX_PATH_NOT_MOUNTED, "nope"))
    processor = CommandProcessor(persistence, publisher, runtime)  # type: ignore[arg-type]
    processor._worker_id = "worker-1"  # pyright: ignore[reportPrivateUsage]

    await processor._handle_command(claimed)  # pyright: ignore[reportPrivateUsage]
    await processor.stop()

    assert persistence.commands[claimed.id].status is CommandStatus.SETTLED
    final = await persistence.get_worker_snapshot(state.conversation.id)
    assert final.active_turn is None
    assert final.queued_turn is None
    assert final.idle_reap_eligible is True
    failed = {
        event.payload.turn_id
        for event in publisher.events
        if isinstance(event.payload, TurnFailedPayload)
    }
    assert failed == {started.state.active_turn.id, second.state.queued_turn.id}


@pytest.mark.asyncio
async def test_startup_failure_cancels_lease_keepalive_before_settling() -> None:
    """The keepalive must be gone before the settle commit; otherwise a renew
    tick after the commit fails, cancels the task, and skips the publish."""
    now, state = _bound_state()
    submitted = submit_turn(state, prompt="hello", idempotency_key="k1", now=now)
    assert submitted.command is not None
    persistence = MemoryPersistence()
    persistence.seed(submitted.state)
    await persistence.accept_command(submitted.command)
    claimed = _claimed(submitted.command, now)
    persistence.commands[claimed.id] = claimed

    publisher = _Publisher()
    runtime = _FailingStartRuntime(DomainError(ErrorCode.SANDBOX_UNAVAILABLE, "gone"), delay=0.05)
    processor = CommandProcessor(
        persistence,
        publisher,
        runtime,  # type: ignore[arg-type]
        lease_seconds=0.03,
    )
    processor._worker_id = "worker-1"  # pyright: ignore[reportPrivateUsage]

    keepalive_alive_at_settle: list[bool] = []
    original_commit = persistence.commit_turn_batch

    async def observing_commit(*args: Any, **kwargs: Any) -> Any:
        keepalive_alive_at_settle.append(
            any(
                not task.done()
                for task in asyncio.all_tasks()
                if (task.get_name() or "").startswith("lease-")
            )
        )
        return await original_commit(*args, **kwargs)

    persistence.commit_turn_batch = observing_commit  # type: ignore[method-assign]

    await processor._handle_command(claimed)  # pyright: ignore[reportPrivateUsage]
    await processor.stop()

    assert keepalive_alive_at_settle == [False]
    assert persistence.commands[claimed.id].status is CommandStatus.SETTLED
    assert any(isinstance(event.payload, TurnFailedPayload) for event in publisher.events)


@pytest.mark.asyncio
async def test_stale_owner_cancels_sibling_tasks_and_refuses_unfenced_writes() -> None:
    _, state = _bound_state()
    conversation_id = state.conversation.id
    persistence = MemoryPersistence()
    persistence.seed(state)
    processor = CommandProcessor(persistence, _Publisher(), _Runtime(persistence, _Adapter()))  # type: ignore[arg-type]
    processor._worker_id = "worker-1"  # pyright: ignore[reportPrivateUsage]
    processor.set_fence(conversation_id, 7)

    started = asyncio.Event()

    async def in_flight() -> None:
        started.set()
        await asyncio.sleep(10)

    sibling_id = uuid4()
    task = asyncio.create_task(in_flight())
    processor._command_tasks[sibling_id] = task  # pyright: ignore[reportPrivateUsage]
    processor._task_conversations[sibling_id] = conversation_id  # pyright: ignore[reportPrivateUsage]
    await started.wait()

    await processor._on_stale_owner(conversation_id)  # pyright: ignore[reportPrivateUsage]

    assert task.cancelled()
    with pytest.raises(DomainError) as exc:
        processor._fence_kwargs(conversation_id)  # pyright: ignore[reportPrivateUsage]
    assert exc.value.code is ErrorCode.STALE_OWNER

    # A fresh claim installs a new fence and lifts the refusal.
    processor.set_fence(conversation_id, 8)
    assert processor._fence_kwargs(conversation_id) == {  # pyright: ignore[reportPrivateUsage]
        "worker_id": "worker-1",
        "fence": 8,
    }
    await processor.stop()
