"""Minimal WorkerCoordinator tests (SQLite singleton refuse + recovery apply paths)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from tests.runtime.memory_persistence import MemoryPersistence

from talktoharnesses.application.command_processor import CommandProcessor
from talktoharnesses.application.persistence import RecoveryAttempt
from talktoharnesses.application.recovery import RecoveryDecision, RecoveryDecisionKind
from talktoharnesses.application.worker_coordinator import WorkerCoordinator
from talktoharnesses.domain.enums import (
    CommandKind,
    CommandStatus,
    ConversationStatus,
    ErrorCode,
    HarnessKind,
    ObservedDeliveryPhase,
    RecoveryAction,
    RecoveryReasonCode,
    RecoveryResultCode,
    RecoveryTrigger,
    TurnStatus,
)
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import ConversationEvent
from talktoharnesses.domain.models import (
    Command,
    ConversationHarnessBinding,
    HarnessCapabilities,
    HarnessConfiguration,
    LaunchSnapshot,
    SubmitTurnPayload,
    Turn,
)
from talktoharnesses.domain.transitions import new_conversation_state
from talktoharnesses.providers.registry import AdapterRegistry
from talktoharnesses.runtime.manager import RuntimeManager
from talktoharnesses.runtime.policy import RuntimePolicy


def _now() -> datetime:
    return datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


class _Publisher:
    async def publish(self, events: Sequence[ConversationEvent]) -> None:
        return None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def _coordinator(
    persistence: MemoryPersistence | None = None,
) -> tuple[WorkerCoordinator, MemoryPersistence, RuntimeManager]:
    p = persistence or MemoryPersistence()
    registry = AdapterRegistry()
    publisher = _Publisher()
    runtime = RuntimeManager(p, registry, clock=_now, policy=RuntimePolicy())
    processor = CommandProcessor(p, publisher, runtime, clock=_now)  # type: ignore[arg-type]
    coordinator = WorkerCoordinator(
        p,
        runtime,
        publisher,  # type: ignore[arg-type]
        processor,
        _now,
        RuntimePolicy(),
        database_system="sqlite",
    )
    return coordinator, p, runtime


def _launch() -> LaunchSnapshot:
    return LaunchSnapshot(
        resolved_executable="/bin/true",
        harness_version="1.0.0",
        working_directory="/tmp/ws",
        adapter_version="0",
        capabilities=HarnessCapabilities(
            kind=HarnessKind.GROK,
            version="1.0.0",
            supports_resume=True,
        ),
    )


def _seed_live_state(
    persistence: MemoryPersistence,
    *,
    command_status: CommandStatus = CommandStatus.DELIVERED,
    native_session_id: str | None = "native-1",
    requires_recreation: bool = False,
    with_binding: bool = True,
) -> tuple[UUID, UUID, UUID]:
    turn_id = uuid4()
    command_id = uuid4()
    conversation_id = uuid4()
    binding = None
    if with_binding:
        binding = ConversationHarnessBinding(
            conversation_id=conversation_id,
            kind=HarnessKind.GROK,
            configuration=HarnessConfiguration(
                kind=HarnessKind.GROK,
                working_directory="/tmp/ws",
            ),
            native_session_id=native_session_id,
            requires_session_recreation=requires_recreation,
            launch_snapshot=_launch(),
            created_at=_now(),
        )
    state = new_conversation_state(
        owner_id="owner",
        now=_now(),
        binding=binding,
        conversation_id=conversation_id,
    )
    turn = Turn(
        id=turn_id,
        conversation_id=conversation_id,
        command_id=command_id,
        status=TurnStatus.RUNNING,
        created_at=_now(),
        started_at=_now(),
    )
    command = Command(
        id=command_id,
        conversation_id=conversation_id,
        kind=CommandKind.SUBMIT_TURN,
        status=command_status,
        idempotency_key=str(uuid4()),
        target_turn_id=turn_id,
        payload=SubmitTurnPayload(prompt="hi"),
        created_at=_now(),
        delivered_at=_now() if command_status is CommandStatus.DELIVERED else None,
        delivery_started_at=(_now() if command_status is CommandStatus.DELIVERY_STARTED else None),
    )
    conversation = state.conversation.model_copy(
        update={
            "status": ConversationStatus.RUNNING,
            "active_turn_id": turn_id,
        }
    )
    persistence.seed(
        state.model_copy(
            update={
                "conversation": conversation,
                "active_turn": turn,
                "commands": {command_id: command},
            }
        )
    )
    return conversation_id, command_id, turn_id


def _own(
    coordinator: WorkerCoordinator,
    persistence: MemoryPersistence,
    conversation_id: UUID,
    *,
    worker_id: str = "worker-a",
) -> int:
    """Mark ownership without starting the heartbeat loop (avoids async teardown hangs)."""
    coordinator._worker_id = worker_id  # pyright: ignore[reportPrivateUsage]
    coordinator._lease_healthy = True  # pyright: ignore[reportPrivateUsage]
    coordinator._heartbeat_healthy = True  # pyright: ignore[reportPrivateUsage]
    coordinator._processor.initialize_worker(worker_id)  # pyright: ignore[reportPrivateUsage]
    fence = 1
    # MemoryPersistence ownership checks use wall-clock now(), not the coordinator clock.
    persistence.ownership[conversation_id] = (
        worker_id,
        fence,
        datetime.now(UTC) + timedelta(hours=1),
    )
    coordinator._fences[conversation_id] = fence  # pyright: ignore[reportPrivateUsage]
    coordinator._processor.set_fence(conversation_id, fence)  # pyright: ignore[reportPrivateUsage]
    return fence


@pytest.mark.asyncio
async def test_sqlite_singleton_refuses_second_worker() -> None:
    persistence = MemoryPersistence()
    first, _, _ = _coordinator(persistence)
    await first.acquire_and_heartbeat("worker-a")

    second, _, _ = _coordinator(persistence)
    with pytest.raises(DomainError) as exc:
        await second.acquire_and_heartbeat("worker-b")
    assert exc.value.code is ErrorCode.WORKER_LEASE_UNAVAILABLE


@pytest.mark.asyncio
async def test_same_worker_reacquire_is_idempotent() -> None:
    coordinator, _, _ = _coordinator()
    await coordinator.acquire_and_heartbeat("worker-a")
    await coordinator.acquire_and_heartbeat("worker-a")
    assert coordinator.worker_id == "worker-a"
    assert coordinator.heartbeat_healthy is True


@pytest.mark.asyncio
async def test_initial_recovery_marks_ready_bits() -> None:
    coordinator, _, _ = _coordinator()
    await coordinator.acquire_and_heartbeat("worker-a")
    assert coordinator.ready_for_work is False
    await coordinator.run_initial_recovery()
    assert coordinator.initial_recovery_complete is True
    assert coordinator.ready_for_work is False
    await coordinator._processor.start("worker-a")  # pyright: ignore[reportPrivateUsage]
    assert coordinator.ready_for_work is True
    snap = coordinator.readiness_snapshot()
    assert snap["recovery_complete"] is True
    assert snap["worker_lease"] is True
    await coordinator._processor.stop()  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_apply_native_resume_success() -> None:
    persistence = MemoryPersistence()
    cid, command_id, turn_id = _seed_live_state(persistence)
    coordinator, _, runtime = _coordinator(persistence)
    fence = _own(coordinator, persistence, cid)
    runtime.prepare_launch_snapshot = AsyncMock(return_value=_launch())  # type: ignore[method-assign]
    runtime.resume_for_recovery = AsyncMock(  # type: ignore[method-assign]
        return_value=(SimpleNamespace(conversation_id=cid), RecoveryReasonCode.UNCHANGED_LAUNCH)
    )

    await coordinator._recover_owned(  # pyright: ignore[reportPrivateUsage]
        cid,
        fence,
        attempt_id=None,
        trigger=RecoveryTrigger.STARTUP,
    )
    runtime.resume_for_recovery.assert_awaited_once()
    resume_await = runtime.resume_for_recovery.await_args
    assert resume_await is not None
    assert resume_await.kwargs["fence"] == fence
    assert cid in coordinator.owned_fences
    # Delivered live turn remains delivered after successful resume.
    assert persistence.states[cid].commands[command_id].status is CommandStatus.DELIVERED
    active_turn = persistence.states[cid].active_turn
    assert active_turn is not None
    assert active_turn.id == turn_id


@pytest.mark.asyncio
async def test_native_resume_failure_falls_through_to_handoff() -> None:
    persistence = MemoryPersistence()
    cid, _, _ = _seed_live_state(persistence)
    coordinator, _, runtime = _coordinator(persistence)
    fence = _own(coordinator, persistence, cid)
    runtime.prepare_launch_snapshot = AsyncMock(return_value=_launch())  # type: ignore[method-assign]
    runtime.resume_for_recovery = AsyncMock(  # type: ignore[method-assign]
        side_effect=DomainError(ErrorCode.PROVIDER_INCOMPATIBLE, "resume rejected")
    )
    candidate = SimpleNamespace(
        session=SimpleNamespace(native_session_id="rotated-native"),
        launch=_launch(),
    )
    runtime.recovery_handoff_fallback = AsyncMock(return_value=candidate)  # type: ignore[method-assign]
    runtime.close_candidate = AsyncMock()  # type: ignore[method-assign]
    runtime.promote_candidate = AsyncMock()  # type: ignore[method-assign]

    await coordinator._recover_owned(  # pyright: ignore[reportPrivateUsage]
        cid,
        fence,
        attempt_id=None,
        trigger=RecoveryTrigger.STARTUP,
    )
    runtime.recovery_handoff_fallback.assert_awaited_once()
    # Live work is terminalized before handoff, then session rotates.
    binding = persistence.states[cid].binding
    assert binding is not None
    assert binding.native_session_id == "rotated-native"


@pytest.mark.asyncio
async def test_apply_outcome_unknown_marks_command() -> None:
    persistence = MemoryPersistence()
    cid, command_id, _ = _seed_live_state(
        persistence,
        command_status=CommandStatus.DELIVERY_STARTED,
    )
    coordinator, _, runtime = _coordinator(persistence)
    fence = _own(coordinator, persistence, cid)
    runtime.prepare_launch_snapshot = AsyncMock(return_value=_launch())  # type: ignore[method-assign]

    await coordinator._recover_owned(  # pyright: ignore[reportPrivateUsage]
        cid,
        fence,
        attempt_id=None,
        trigger=RecoveryTrigger.TAKEOVER,
    )
    assert persistence.states[cid].commands[command_id].status is CommandStatus.OUTCOME_UNKNOWN
    assert persistence.states[cid].active_turn is None


@pytest.mark.asyncio
async def test_handoff_fallback_without_binding_fails_attempt() -> None:
    persistence = MemoryPersistence()
    cid, command_id, turn_id = _seed_live_state(persistence, with_binding=False)
    coordinator, _, runtime = _coordinator(persistence)
    fence = _own(coordinator, persistence, cid)
    attempt_id = uuid4()
    persistence.recovery_attempts[attempt_id] = RecoveryAttempt(
        id=attempt_id,
        conversation_id=cid,
        binding_id=uuid4(),
        command_id=command_id,
        turn_id=turn_id,
        worker_id="worker-a",
        fence=fence,
        trigger=RecoveryTrigger.STARTUP.value,
        observed_delivery_phase=ObservedDeliveryPhase.NONE.value,
        action=RecoveryAction.HANDOFF_FALLBACK.value,
        result=None,
        reason_code=RecoveryReasonCode.RECOVERY_FALLBACK.value,
        started_at=_now(),
        completed_at=None,
    )
    runtime.recovery_handoff_fallback = AsyncMock()  # type: ignore[method-assign]
    # Force handoff path directly: no binding => failed attempt.
    state = await persistence.get_worker_snapshot(cid)
    await coordinator._apply_decision(  # pyright: ignore[reportPrivateUsage]
        state,
        RecoveryDecision(
            kind=RecoveryDecisionKind.HANDOFF_FALLBACK,
            action=RecoveryAction.HANDOFF_FALLBACK,
            reason_code=RecoveryReasonCode.RECOVERY_FALLBACK,
            observed_delivery_phase=ObservedDeliveryPhase.NONE,
            command_id=command_id,
            turn_id=turn_id,
        ),
        fence=fence,
        attempt_id=attempt_id,
        trigger=RecoveryTrigger.STARTUP,
    )
    assert persistence.recovery_attempts[attempt_id].result == RecoveryResultCode.FAILED.value
    runtime.recovery_handoff_fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_handoff_fallback_with_binding_rotates_session() -> None:
    persistence = MemoryPersistence()
    cid, _, _ = _seed_live_state(persistence, requires_recreation=True)
    coordinator, _, runtime = _coordinator(persistence)
    fence = _own(coordinator, persistence, cid)
    runtime.prepare_launch_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=LaunchSnapshot(
            resolved_executable="/bin/true",
            harness_version="1.0.0",
            working_directory="/tmp/ws",
            adapter_version="0",
            capabilities=HarnessCapabilities(
                kind=HarnessKind.GROK,
                version="1.0.0",
                supports_resume=False,
            ),
        )
    )
    candidate = SimpleNamespace(
        session=SimpleNamespace(native_session_id="handoff-native"),
        launch=_launch(),
    )
    runtime.recovery_handoff_fallback = AsyncMock(return_value=candidate)  # type: ignore[method-assign]
    runtime.close_candidate = AsyncMock()  # type: ignore[method-assign]
    runtime.promote_candidate = AsyncMock()  # type: ignore[method-assign]

    await coordinator._recover_owned(  # pyright: ignore[reportPrivateUsage]
        cid,
        fence,
        attempt_id=None,
        trigger=RecoveryTrigger.STARTUP,
    )
    runtime.recovery_handoff_fallback.assert_awaited_once()
    binding = persistence.states[cid].binding
    assert binding is not None
    assert binding.native_session_id == "handoff-native"


@pytest.mark.asyncio
async def test_native_resume_stale_owner_drops_lease() -> None:
    persistence = MemoryPersistence()
    cid, _, _ = _seed_live_state(persistence)
    coordinator, _, runtime = _coordinator(persistence)
    fence = _own(coordinator, persistence, cid)
    runtime.prepare_launch_snapshot = AsyncMock(return_value=_launch())  # type: ignore[method-assign]
    runtime.resume_for_recovery = AsyncMock(  # type: ignore[method-assign]
        side_effect=DomainError(ErrorCode.STALE_OWNER, "stale")
    )
    runtime.close = AsyncMock()  # type: ignore[method-assign]

    await coordinator._recover_owned(  # pyright: ignore[reportPrivateUsage]
        cid,
        fence,
        attempt_id=None,
        trigger=RecoveryTrigger.STARTUP,
    )
    assert cid not in coordinator.owned_fences
    runtime.close.assert_awaited()


@pytest.mark.asyncio
async def test_apply_no_action_and_reclaim_complete_attempts() -> None:
    persistence = MemoryPersistence()
    cid, command_id, turn_id = _seed_live_state(persistence)
    # Idle conversation with accepted command → leave claimable / no action paths.
    state = persistence.states[cid]
    persistence.states[cid] = state.model_copy(
        update={
            "active_turn": None,
            "conversation": state.conversation.model_copy(
                update={
                    "status": ConversationStatus.IDLE,
                    "active_turn_id": None,
                }
            ),
            "commands": {
                command_id: state.commands[command_id].model_copy(
                    update={
                        "status": CommandStatus.ACCEPTED,
                        "delivered_at": None,
                        "target_turn_id": None,
                    }
                )
            },
        }
    )
    coordinator, _, runtime = _coordinator(persistence)
    fence = _own(coordinator, persistence, cid)
    runtime.prepare_launch_snapshot = AsyncMock(return_value=_launch())  # type: ignore[method-assign]
    attempt_id = uuid4()
    persistence.recovery_attempts[attempt_id] = RecoveryAttempt(
        id=attempt_id,
        conversation_id=cid,
        binding_id=state.binding.id if state.binding else uuid4(),
        command_id=command_id,
        turn_id=turn_id,
        worker_id="worker-a",
        fence=fence,
        trigger=RecoveryTrigger.STARTUP.value,
        observed_delivery_phase=ObservedDeliveryPhase.NONE.value,
        action=RecoveryAction.NO_ACTION.value,
        result=None,
        reason_code=RecoveryReasonCode.NO_ACTION.value,
        started_at=_now(),
        completed_at=None,
    )
    await coordinator._recover_owned(  # pyright: ignore[reportPrivateUsage]
        cid,
        fence,
        attempt_id=attempt_id,
        trigger=RecoveryTrigger.STARTUP,
    )
    assert persistence.recovery_attempts[attempt_id].result in {
        RecoveryResultCode.NO_ACTION.value,
        RecoveryResultCode.SUCCESS.value,
    }


@pytest.mark.asyncio
async def test_handoff_fallback_failure_when_candidate_rejected() -> None:
    persistence = MemoryPersistence()
    cid, _, _ = _seed_live_state(persistence, requires_recreation=True)
    coordinator, _, runtime = _coordinator(persistence)
    fence = _own(coordinator, persistence, cid)
    runtime.prepare_launch_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=LaunchSnapshot(
            resolved_executable="/bin/true",
            harness_version="1.0.0",
            working_directory="/tmp/ws",
            adapter_version="0",
            capabilities=HarnessCapabilities(
                kind=HarnessKind.GROK,
                version="1.0.0",
                supports_resume=False,
            ),
        )
    )
    runtime.recovery_handoff_fallback = AsyncMock(return_value=None)  # type: ignore[method-assign]
    attempt_id = uuid4()
    binding_id = persistence.states[cid].binding.id  # type: ignore[union-attr]
    persistence.recovery_attempts[attempt_id] = RecoveryAttempt(
        id=attempt_id,
        conversation_id=cid,
        binding_id=binding_id,
        command_id=None,
        turn_id=None,
        worker_id="worker-a",
        fence=fence,
        trigger=RecoveryTrigger.STARTUP.value,
        observed_delivery_phase=ObservedDeliveryPhase.NONE.value,
        action=RecoveryAction.HANDOFF_FALLBACK.value,
        result=None,
        reason_code=RecoveryReasonCode.RECOVERY_FALLBACK.value,
        started_at=_now(),
        completed_at=None,
    )
    await coordinator._recover_owned(  # pyright: ignore[reportPrivateUsage]
        cid,
        fence,
        attempt_id=attempt_id,
        trigger=RecoveryTrigger.STARTUP,
    )
    assert persistence.recovery_attempts[attempt_id].result == RecoveryResultCode.FAILED.value


@pytest.mark.asyncio
async def test_begin_shutdown_terminalizes_inflight_delivery() -> None:
    persistence = MemoryPersistence()
    cid, command_id, _ = _seed_live_state(
        persistence,
        command_status=CommandStatus.DELIVERY_STARTED,
    )
    coordinator, _, runtime = _coordinator(persistence)
    _own(coordinator, persistence, cid)
    runtime.close = AsyncMock()  # type: ignore[method-assign]
    await coordinator.begin_shutdown(deadline=0.0)
    assert coordinator.draining is True
    assert persistence.states[cid].commands[command_id].status is CommandStatus.OUTCOME_UNKNOWN
    await coordinator.finish_shutdown()
    assert coordinator.heartbeat_healthy is False


@pytest.mark.asyncio
async def test_probe_failure_and_resume_generic_exception_fall_to_handoff() -> None:
    persistence = MemoryPersistence()
    cid, _, _ = _seed_live_state(persistence)
    coordinator, _, runtime = _coordinator(persistence)
    fence = _own(coordinator, persistence, cid)
    runtime.prepare_launch_snapshot = AsyncMock(side_effect=RuntimeError("probe boom"))  # type: ignore[method-assign]
    candidate = SimpleNamespace(
        session=SimpleNamespace(native_session_id="after-probe-fail"),
        launch=_launch(),
    )
    runtime.recovery_handoff_fallback = AsyncMock(return_value=candidate)  # type: ignore[method-assign]
    runtime.close_candidate = AsyncMock()  # type: ignore[method-assign]
    runtime.promote_candidate = AsyncMock()  # type: ignore[method-assign]
    await coordinator._recover_owned(  # pyright: ignore[reportPrivateUsage]
        cid,
        fence,
        attempt_id=None,
        trigger=RecoveryTrigger.STARTUP,
    )
    # supports_resume=False after probe failure → handoff fallback.
    runtime.recovery_handoff_fallback.assert_awaited_once()

    persistence2 = MemoryPersistence()
    cid2, _, _ = _seed_live_state(persistence2)
    coordinator2, _, runtime2 = _coordinator(persistence2)
    fence2 = _own(coordinator2, persistence2, cid2)
    runtime2.prepare_launch_snapshot = AsyncMock(return_value=_launch())  # type: ignore[method-assign]
    runtime2.resume_for_recovery = AsyncMock(side_effect=RuntimeError("resume boom"))  # type: ignore[method-assign]
    runtime2.recovery_handoff_fallback = AsyncMock(return_value=candidate)  # type: ignore[method-assign]
    runtime2.close_candidate = AsyncMock()  # type: ignore[method-assign]
    runtime2.promote_candidate = AsyncMock()  # type: ignore[method-assign]
    await coordinator2._recover_owned(  # pyright: ignore[reportPrivateUsage]
        cid2,
        fence2,
        attempt_id=None,
        trigger=RecoveryTrigger.STARTUP,
    )
    runtime2.recovery_handoff_fallback.assert_awaited_once()


@pytest.mark.asyncio
async def test_release_undelivered_claims_and_worker_lease_lost() -> None:
    persistence = MemoryPersistence()
    cid, command_id, _ = _seed_live_state(
        persistence,
        command_status=CommandStatus.CLAIMED,
    )
    state = persistence.states[cid]
    cmd = state.commands[command_id].model_copy(
        update={
            "status": CommandStatus.CLAIMED,
            "worker_id": "worker-a",
            "delivery_started_at": None,
            "lease_expires_at": _now() + timedelta(minutes=1),
        }
    )
    persistence.states[cid] = state.model_copy(update={"commands": {command_id: cmd}})
    coordinator, _, runtime = _coordinator(persistence)
    fence = _own(coordinator, persistence, cid)
    coordinator._worker_id = "worker-a"  # pyright: ignore[reportPrivateUsage]
    await coordinator._release_undelivered_claims()  # pyright: ignore[reportPrivateUsage]
    released = persistence.states[cid].commands[command_id]
    assert released.status is CommandStatus.ACCEPTED
    assert released.worker_id is None

    runtime.close = AsyncMock()  # type: ignore[method-assign]
    runtime.shutdown = AsyncMock()  # type: ignore[method-assign]
    coordinator._fences[cid] = fence  # pyright: ignore[reportPrivateUsage]
    await coordinator._on_worker_lease_lost()  # pyright: ignore[reportPrivateUsage]
    assert coordinator._lease_healthy is False  # pyright: ignore[reportPrivateUsage]
    assert cid not in coordinator._fences  # pyright: ignore[reportPrivateUsage]
    runtime.close.assert_awaited()
    runtime.shutdown.assert_awaited()


@pytest.mark.asyncio
async def test_native_resume_without_native_session_returns_false_path() -> None:
    persistence = MemoryPersistence()
    cid, _, _ = _seed_live_state(persistence, native_session_id=None)
    coordinator, _, runtime = _coordinator(persistence)
    fence = _own(coordinator, persistence, cid)
    runtime.prepare_launch_snapshot = AsyncMock(return_value=_launch())  # type: ignore[method-assign]
    candidate = SimpleNamespace(
        session=SimpleNamespace(native_session_id="handoff-after-empty-native"),
        launch=_launch(),
    )
    runtime.recovery_handoff_fallback = AsyncMock(return_value=candidate)  # type: ignore[method-assign]
    runtime.close_candidate = AsyncMock()  # type: ignore[method-assign]
    runtime.promote_candidate = AsyncMock()  # type: ignore[method-assign]
    await coordinator._recover_owned(  # pyright: ignore[reportPrivateUsage]
        cid,
        fence,
        attempt_id=None,
        trigger=RecoveryTrigger.STARTUP,
    )
    runtime.recovery_handoff_fallback.assert_awaited_once()
