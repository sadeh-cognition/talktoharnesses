"""Durable harness switching: acceptance, worker transaction, and compensation."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from tests.runtime.conftest import FakeAdapter, SeedReply
from tests.runtime.memory_persistence import MemoryPersistence

from talktoharnesses.application.service import TalkToHarnessesService
from talktoharnesses.domain import (
    CommandKind,
    CommandStatus,
    DomainError,
    ErrorCode,
    HarnessConfiguration,
    HarnessKind,
    MessageRole,
    append_events,
    start_turn,
    submit_turn,
)
from talktoharnesses.domain.enums import TurnStatus
from talktoharnesses.domain.events import ConversationEvent, ProviderWarningPayload
from talktoharnesses.domain.models import Command, Message, Turn
from talktoharnesses.providers.registry import AdapterRegistry
from talktoharnesses.runtime.manager import RuntimeManager
from talktoharnesses.runtime.policy import RuntimePolicy


def _now() -> datetime:
    return datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)


class _SwitchAdapter(FakeAdapter):
    """SDK-managed fake so switching needs no child process or executable."""

    sdk_managed = True

    def __init__(self, kind: HarnessKind, *, seed_reply: SeedReply = "completed") -> None:
        super().__init__(seed_reply=seed_reply)
        self.kind = kind


class _Publisher:
    def __init__(self) -> None:
        self.events: list[ConversationEvent] = []

    async def publish(self, events: Sequence[ConversationEvent]) -> None:
        self.events.extend(events)


def _config(kind: HarnessKind, workdir: Path) -> HarnessConfiguration:
    return HarnessConfiguration(kind=kind, working_directory=str(workdir))


def _service(
    workdir: Path,
    *,
    target_seed: SeedReply = "completed",
) -> tuple[TalkToHarnessesService, MemoryPersistence, _Publisher, list[_SwitchAdapter]]:
    persistence = MemoryPersistence()
    adapters: list[_SwitchAdapter] = []

    def factory(kind: HarnessKind, seed_reply: SeedReply) -> _SwitchAdapter:
        adapter = _SwitchAdapter(kind, seed_reply=seed_reply)
        adapters.append(adapter)
        return adapter

    registry = AdapterRegistry()
    registry.register(HarnessKind.GROK, lambda: factory(HarnessKind.GROK, "completed"))  # type: ignore[arg-type]
    registry.register(HarnessKind.CODEX, lambda: factory(HarnessKind.CODEX, target_seed))  # type: ignore[arg-type]
    publisher = _Publisher()
    runtime = RuntimeManager(
        persistence,
        registry,
        policy=RuntimePolicy(start_resume_timeout=2.0, graceful_close_timeout=0.3),
        clock=_now,
    )
    service = TalkToHarnessesService(persistence, registry, publisher, _now, runtime)
    return service, persistence, publisher, adapters


async def _conversation_on_grok(
    service: TalkToHarnessesService,
    workdir: Path,
) -> tuple[UUID, UUID]:
    """Create a probed GROK conversation and a probed CODEX switch target."""
    source = await service.create_harness(
        "owner",
        name="a",
        configuration=_config(HarnessKind.GROK, workdir),
    )
    await service.probe_harness("owner", source.id)
    target = await service.create_harness(
        "owner",
        name="b",
        configuration=_config(HarnessKind.CODEX, workdir),
    )
    await service.probe_harness("owner", target.id)
    snapshot = await service.create_conversation("owner", source.id)
    return snapshot.detail.conversation.id, target.id


def _seed_retained_turn(persistence: MemoryPersistence, conversation_id: UUID) -> None:
    """Give the conversation one completed turn so the handoff is non-empty."""
    turn_id = uuid4()
    message_id = uuid4()
    persistence.turns[conversation_id][turn_id] = Turn(
        id=turn_id,
        conversation_id=conversation_id,
        status=TurnStatus.COMPLETED,
        user_message_id=message_id,
        created_at=_now(),
        completed_at=_now(),
    )
    persistence.turn_order[conversation_id].append(turn_id)
    persistence.messages[conversation_id][message_id] = Message(
        id=message_id,
        turn_id=turn_id,
        role=MessageRole.USER,
        text="build the thing",
        created_at=_now(),
    )


async def _claim(persistence: MemoryPersistence, kind: CommandKind) -> Command:
    claimed = await persistence.claim_commands("worker-1", 8, lease_duration=30.0)
    return next(item.command for item in claimed if item.command.kind is kind)


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_acceptance_is_idempotent(tmp_path: Path) -> None:
    service, persistence, publisher, _adapters = _service(tmp_path)
    cid, target_id = await _conversation_on_grok(service, tmp_path)

    accepted = await service.switch_harness(
        "owner", cid, harness_id=target_id, idempotency_key="k1"
    )
    assert accepted.kind is CommandKind.SWITCH_HARNESS
    assert accepted.status is CommandStatus.ACCEPTED
    published = len(publisher.events)

    again = await service.switch_harness("owner", cid, harness_id=target_id, idempotency_key="k1")
    assert again.id == accepted.id
    assert len(publisher.events) == published
    switches = [c for c in persistence.commands.values() if c.kind is CommandKind.SWITCH_HARNESS]
    assert len(switches) == 1


@pytest.mark.asyncio
async def test_switch_key_reuse_with_other_target_conflicts(tmp_path: Path) -> None:
    service, _persistence, _publisher, _adapters = _service(tmp_path)
    cid, target_id = await _conversation_on_grok(service, tmp_path)
    other = await service.create_harness(
        "owner",
        name="c",
        configuration=_config(HarnessKind.CODEX, tmp_path),
    )
    await service.probe_harness("owner", other.id)

    await service.switch_harness("owner", cid, harness_id=target_id, idempotency_key="k1")
    with pytest.raises(DomainError) as exc:
        await service.switch_harness("owner", cid, harness_id=other.id, idempotency_key="k1")
    assert exc.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


@pytest.mark.asyncio
async def test_switch_requires_idle_conversation_and_owned_target(tmp_path: Path) -> None:
    service, persistence, _publisher, _adapters = _service(tmp_path)
    cid, target_id = await _conversation_on_grok(service, tmp_path)

    with pytest.raises(DomainError) as missing_key:
        await service.switch_harness("owner", cid, harness_id=target_id, idempotency_key=" ")
    assert missing_key.value.code is ErrorCode.INVALID_STATE

    with pytest.raises(DomainError) as cross_owner:
        await service.switch_harness("other", cid, harness_id=target_id, idempotency_key="k1")
    assert cross_owner.value.code in {ErrorCode.NOT_FOUND, ErrorCode.INVALID_STATE}

    await service.submit_turn("owner", cid, prompt="hi", idempotency_key="turn-1")
    with pytest.raises(DomainError) as busy:
        await service.switch_harness("owner", cid, harness_id=target_id, idempotency_key="k1")
    assert busy.value.code is ErrorCode.CONVERSATION_BUSY
    assert all(c.kind is not CommandKind.SWITCH_HARNESS for c in persistence.commands.values())


@pytest.mark.asyncio
async def test_switch_target_requires_successful_probe(tmp_path: Path) -> None:
    service, _persistence, _publisher, _adapters = _service(tmp_path)
    cid, _target_id = await _conversation_on_grok(service, tmp_path)
    unprobed = await service.create_harness(
        "owner",
        name="unprobed",
        configuration=_config(HarnessKind.CODEX, tmp_path),
    )

    with pytest.raises(DomainError) as exc:
        await service.switch_harness("owner", cid, harness_id=unprobed.id, idempotency_key="k1")
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


# ---------------------------------------------------------------------------
# Worker transaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_switch_commits_new_binding_and_promotes_candidate(tmp_path: Path) -> None:
    service, persistence, publisher, adapters = _service(tmp_path)
    cid, target_id = await _conversation_on_grok(service, tmp_path)
    _seed_retained_turn(persistence, cid)

    state = await persistence.get_worker_snapshot(cid)
    assert state.binding is not None
    previous_binding = state.binding
    await service._runtime.start(  # pyright: ignore[reportPrivateUsage]
        conversation_id=cid,
        owner_id="owner",
        configuration=previous_binding.configuration,
        argv=(),
    )
    previous_runtime = service._runtime.get_runtime(cid)  # pyright: ignore[reportPrivateUsage]
    assert previous_runtime is not None

    await service.switch_harness("owner", cid, harness_id=target_id, idempotency_key="k1")
    claimed = await _claim(persistence, CommandKind.SWITCH_HARNESS)
    service.processor._worker_id = "worker-1"  # pyright: ignore[reportPrivateUsage]
    created = len(adapters)
    await service.processor._execute_command(claimed)  # pyright: ignore[reportPrivateUsage]

    switched = await persistence.get_worker_snapshot(cid)
    assert switched.binding is not None
    assert switched.binding.kind is HarnessKind.CODEX
    assert switched.binding.id != previous_binding.id
    assert switched.binding.harness_instance_id == target_id
    assert switched.conversation.current_binding_id == switched.binding.id
    # A new binding never carries the previous native session identity.
    assert switched.binding.native_session_id is not None
    assert switched.binding.native_session_id != previous_binding.native_session_id

    assert [e.type for e in publisher.events if e.type.startswith("harness_switch")] == [
        "harness_switched"
    ]
    assert persistence.commands[claimed.id].status is CommandStatus.SETTLED

    promoted = service._runtime.get_runtime(cid)  # pyright: ignore[reportPrivateUsage]
    assert promoted is not None
    assert promoted.session.binding_id == switched.binding.id
    assert promoted is not previous_runtime
    assert previous_runtime.closed is True

    # The candidate received exactly the rendered retained handoff.
    (candidate_adapter,) = adapters[created:]
    assert candidate_adapter.kind is HarnessKind.CODEX
    assert [r.prompt for r in candidate_adapter.submissions] == ["[user]: build the thing"]

    await service.stop()


@pytest.mark.asyncio
async def test_rejected_candidate_keeps_the_current_binding(tmp_path: Path) -> None:
    service, persistence, publisher, adapters = _service(tmp_path, target_seed="failed")
    cid, target_id = await _conversation_on_grok(service, tmp_path)
    _seed_retained_turn(persistence, cid)
    before = await persistence.get_worker_snapshot(cid)
    assert before.binding is not None

    await service.switch_harness("owner", cid, harness_id=target_id, idempotency_key="k1")
    claimed = await _claim(persistence, CommandKind.SWITCH_HARNESS)
    service.processor._worker_id = "worker-1"  # pyright: ignore[reportPrivateUsage]
    created = len(adapters)
    await service.processor._execute_command(claimed)  # pyright: ignore[reportPrivateUsage]

    after = await persistence.get_worker_snapshot(cid)
    assert after.binding is not None
    assert after.binding.id == before.binding.id
    assert after.binding.kind is HarnessKind.GROK
    assert [e.type for e in publisher.events if e.type.startswith("harness_switch")] == [
        "harness_switch_failed"
    ]
    assert persistence.commands[claimed.id].status is CommandStatus.SETTLED

    (candidate_adapter,) = adapters[created:]
    assert candidate_adapter.closed is True
    assert service._runtime.get_runtime(cid) is None  # pyright: ignore[reportPrivateUsage]

    await service.stop()


@pytest.mark.asyncio
async def test_switch_rejects_candidate_when_prepared_version_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, persistence, publisher, adapters = _service(tmp_path)
    cid, target_id = await _conversation_on_grok(service, tmp_path)
    _seed_retained_turn(persistence, cid)
    before = await persistence.get_worker_snapshot(cid)
    assert before.binding is not None

    original_seed = service._runtime.seed_candidate  # pyright: ignore[reportPrivateUsage]

    async def seed_then_change_version(candidate: object, handoff: str) -> None:
        await original_seed(candidate, handoff)  # type: ignore[arg-type]
        persistence.messages[cid].clear()
        current = await persistence.get_worker_snapshot(cid)
        changed = append_events(
            current,
            _now(),
            [ProviderWarningPayload(message="concurrent retention commit")],
        )
        await persistence.commit_turn_batch(
            cid,
            current.conversation.version,
            changed[0],
            changed[1],
        )

    runtime = service._runtime  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(runtime, "seed_candidate", seed_then_change_version)
    await service.switch_harness("owner", cid, harness_id=target_id, idempotency_key="k1")
    claimed = await _claim(persistence, CommandKind.SWITCH_HARNESS)
    service.processor._worker_id = "worker-1"  # pyright: ignore[reportPrivateUsage]
    created = len(adapters)

    await service.processor._execute_command(claimed)  # pyright: ignore[reportPrivateUsage]

    after = await persistence.get_worker_snapshot(cid)
    assert after.binding is not None
    assert after.binding.id == before.binding.id
    switch_events = [
        event.type for event in publisher.events if event.type.startswith("harness_switch")
    ]
    assert switch_events == ["harness_switch_failed"]
    (candidate_adapter,) = adapters[created:]
    assert candidate_adapter.closed is True
    assert (await persistence.read_retained_handoff(cid)).entries == ()
    await service.stop()


@pytest.mark.asyncio
async def test_switch_renews_lease_during_slow_candidate_seed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, persistence, _publisher, _adapters = _service(tmp_path)
    cid, target_id = await _conversation_on_grok(service, tmp_path)
    _seed_retained_turn(persistence, cid)
    renewals = 0
    original_renew = persistence.renew_command_lease
    original_seed = service._runtime.seed_candidate  # pyright: ignore[reportPrivateUsage]

    async def count_renewal(
        command_id: UUID,
        worker_id: str,
        *,
        lease_duration: float,
        fence: int | None = None,
    ) -> None:
        nonlocal renewals
        renewals += 1
        await original_renew(
            command_id,
            worker_id,
            lease_duration=lease_duration,
            fence=fence,
        )

    async def slow_seed(candidate: object, handoff: str) -> None:
        await asyncio.sleep(0.22)
        await original_seed(candidate, handoff)  # type: ignore[arg-type]

    monkeypatch.setattr(persistence, "renew_command_lease", count_renewal)
    runtime = service._runtime  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(runtime, "seed_candidate", slow_seed)
    service.processor._lease_seconds = 0.03  # pyright: ignore[reportPrivateUsage]
    await service.switch_harness("owner", cid, harness_id=target_id, idempotency_key="k1")
    claimed = await _claim(persistence, CommandKind.SWITCH_HARNESS)
    service.processor._worker_id = "worker-1"  # pyright: ignore[reportPrivateUsage]

    await service.processor._execute_command(claimed)  # pyright: ignore[reportPrivateUsage]

    assert renewals >= 2
    assert persistence.commands[claimed.id].status is CommandStatus.SETTLED
    await service.stop()


@pytest.mark.asyncio
async def test_switch_sanitizes_unexpected_failure_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, persistence, publisher, _adapters = _service(tmp_path)
    cid, target_id = await _conversation_on_grok(service, tmp_path)

    async def fail_candidate(**_kwargs: object) -> object:
        raise RuntimeError("/secret/provider/path: raw payload")

    runtime = service._runtime  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(runtime, "start_candidate", fail_candidate)
    await service.switch_harness("owner", cid, harness_id=target_id, idempotency_key="k1")
    claimed = await _claim(persistence, CommandKind.SWITCH_HARNESS)
    service.processor._worker_id = "worker-1"  # pyright: ignore[reportPrivateUsage]

    await service.processor._execute_command(claimed)  # pyright: ignore[reportPrivateUsage]

    failure = next(event for event in publisher.events if event.type == "harness_switch_failed")
    assert failure.payload.message == "invalid state"  # type: ignore[union-attr]
    settled = persistence.commands[claimed.id]
    assert settled.status is CommandStatus.SETTLED
    assert settled.recovery_attempt_id is None
    assert "/secret" not in failure.model_dump_json()
    assert "raw payload" not in failure.model_dump_json()
    await service.stop()


@pytest.mark.asyncio
async def test_switch_claimed_while_busy_is_released_to_accepted(tmp_path: Path) -> None:
    service, persistence, _publisher, adapters = _service(tmp_path)
    cid, target_id = await _conversation_on_grok(service, tmp_path)

    await service.switch_harness("owner", cid, harness_id=target_id, idempotency_key="k1")
    # A turn starts between acceptance and the worker claim.
    state = await persistence.get_worker_snapshot(cid)
    queued = submit_turn(state, prompt="hi", idempotency_key="turn-1", now=_now())
    running = start_turn(queued.state, now=_now())
    await persistence.commit_turn_batch(
        cid,
        state.conversation.version,
        running.state,
        (*queued.events, *running.events),
        tuple(running.state.commands.values()),
    )

    claimed = await _claim(persistence, CommandKind.SWITCH_HARNESS)
    service.processor._worker_id = "worker-1"  # pyright: ignore[reportPrivateUsage]
    created = len(adapters)
    await service.processor._execute_command(claimed)  # pyright: ignore[reportPrivateUsage]

    released = persistence.commands[claimed.id]
    assert released.status is CommandStatus.ACCEPTED
    assert released.worker_id is None
    # A deferred switch must not create a candidate session.
    assert adapters[created:] == []
    unchanged = await persistence.get_worker_snapshot(cid)
    assert unchanged.binding is not None
    assert unchanged.binding.kind is HarnessKind.GROK
