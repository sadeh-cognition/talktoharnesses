"""Worker lease, fencing, heartbeat, and deterministic recovery (Phase 9 WP2)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import UUID

from talktoharnesses.application.command_processor import CommandProcessor
from talktoharnesses.application.faults import FaultCallback, FaultPoint, checkpoint
from talktoharnesses.application.handoff import render_handoff
from talktoharnesses.application.observability import (
    GAUGE_OWNED_CONVERSATIONS,
    GAUGE_WORKER_READY,
    SPAN_WORKER_RECOVERY,
    get_observability,
)
from talktoharnesses.application.persistence import ConversationOwnership, Persistence
from talktoharnesses.application.publisher import CommittedEventPublisher
from talktoharnesses.application.recovery import (
    RecoveryDecision,
    RecoveryDecisionKind,
    classify_conversation,
    is_switch_command,
    turn_needs_interrupt_messages,
)
from talktoharnesses.domain.enums import (
    ActivityStatus,
    CommandStatus,
    ErrorCode,
    ObservedDeliveryPhase,
    RecoveryAction,
    RecoveryReasonCode,
    RecoveryResultCode,
    RecoveryTrigger,
)
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import Command
from talktoharnesses.domain.transitions import (
    ConversationState,
    fail_running_activities,
    fail_switch,
    mark_outcome_unknown,
)
from talktoharnesses.runtime.manager import RuntimeManager
from talktoharnesses.runtime.policy import RuntimePolicy

logger = logging.getLogger(__name__)

DatabaseSystem = Literal["sqlite", "postgresql"]


def _detect_database_system() -> DatabaseSystem:
    try:
        from django.db import connection

        if connection.vendor == "postgresql":
            return "postgresql"
    except Exception:
        pass
    return "sqlite"


class WorkerCoordinator:
    """Sole owner of the worker lease, fences, heartbeat, and recovery scan."""

    def __init__(
        self,
        persistence: Persistence,
        runtime_manager: RuntimeManager,
        publisher: CommittedEventPublisher,
        command_processor: CommandProcessor,
        clock: Callable[[], datetime],
        policy: RuntimePolicy,
        *,
        database_system: DatabaseSystem | None = None,
        fault_callback: FaultCallback = None,
    ) -> None:
        self._persistence = persistence
        self._runtime = runtime_manager
        self._publisher = publisher
        self._processor = command_processor
        self._clock = clock
        self._policy = policy
        self._database_system: DatabaseSystem = database_system or _detect_database_system()
        self._fault_callback = fault_callback

        self._worker_id: str | None = None
        self._fences: dict[UUID, int] = {}
        self._attempt_ids: dict[UUID, UUID] = {}
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._initial_recovery_complete = False
        self._draining = False
        self._heartbeat_healthy = False
        self._claims_healthy = True
        self._lease_healthy = False

    @property
    def worker_id(self) -> str | None:
        return self._worker_id

    @property
    def owned_fences(self) -> dict[UUID, int]:
        return dict(self._fences)

    @property
    def initial_recovery_complete(self) -> bool:
        return self._initial_recovery_complete

    @property
    def draining(self) -> bool:
        return self._draining

    @property
    def heartbeat_healthy(self) -> bool:
        return self._heartbeat_healthy

    @property
    def claims_healthy(self) -> bool:
        return self._claims_healthy and self._processor.claim_loop_healthy

    @property
    def ready_for_work(self) -> bool:
        return (
            self._initial_recovery_complete
            and not self._draining
            and self._lease_healthy
            and self._heartbeat_healthy
            and self.claims_healthy
        )

    def readiness_snapshot(self) -> dict[str, bool]:
        return {
            "worker_lease": self._lease_healthy,
            "heartbeat": self._heartbeat_healthy,
            "recovery_complete": self._initial_recovery_complete,
            "draining": self._draining,
            "claims_healthy": self.claims_healthy,
            "ready_for_work": self.ready_for_work,
        }

    async def acquire_and_heartbeat(self, worker_id: str) -> None:
        """Acquire the process worker lease and start the heartbeat loop."""
        await self._persistence.acquire_worker_lease(
            worker_id,
            lease_duration=self._policy.lease_duration,
        )
        self._worker_id = worker_id
        self._lease_healthy = True
        self._draining = False
        self._initial_recovery_complete = False
        self._heartbeat_healthy = True
        self._claims_healthy = True
        self._processor.set_claims_enabled(False)
        self._processor.initialize_worker(worker_id)
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(),
                name=f"worker-heartbeat-{worker_id}",
            )

    async def run_initial_recovery(self) -> None:
        """Claim expired conversations and apply durable recovery decisions."""
        assert self._worker_id is not None
        obs = get_observability()
        started = time.perf_counter()
        while self._capacity_remaining() > 0:
            if not await self._recover_batch(trigger=RecoveryTrigger.STARTUP):
                self._initial_recovery_complete = True
                break
        # Also inspect conversations already owned (e.g. renewed after acquire).
        for conversation_id, fence in list(self._fences.items()):
            if conversation_id in self._attempt_ids:
                continue
            attempt = await self._persistence.get_open_recovery_attempt(
                conversation_id,
                self._worker_id,
                fence,
            )
            if attempt is not None:
                self._attempt_ids[conversation_id] = attempt.id
            await self._recover_owned(
                conversation_id,
                fence,
                attempt_id=attempt.id if attempt else None,
                trigger=RecoveryTrigger.STARTUP,
            )
        obs.record_startup_recovery_duration(
            time.perf_counter() - started,
            database_system=self._database_system,
        )
        obs.set_gauge_sample(GAUGE_OWNED_CONVERSATIONS, len(self._fences))
        obs.set_gauge_sample(GAUGE_WORKER_READY, 1.0 if self.ready_for_work else 0.0)

    async def begin_shutdown(self, deadline: float) -> None:
        """Mark draining, stop claims, and release undelivered command claims."""
        _ = deadline
        self._draining = True
        self._processor.begin_shutdown()
        if self._worker_id is not None:
            with contextlib.suppress(Exception):
                await self._persistence.mark_worker_draining(self._worker_id)
        await self._release_undelivered_claims()
        await self._terminalize_inflight_deliveries()

    async def _terminalize_inflight_deliveries(self) -> None:
        if self._worker_id is None:
            return
        for conversation_id, fence in list(self._fences.items()):
            try:
                state = await self._persistence.get_worker_snapshot(conversation_id)
                candidates = sorted(
                    (
                        command
                        for command in state.commands.values()
                        if command.status
                        in {CommandStatus.DELIVERY_STARTED, CommandStatus.DELIVERED}
                        or (
                            command.status is CommandStatus.CLAIMED
                            and command.delivery_started_at is not None
                        )
                    ),
                    key=lambda command: (
                        0 if command.status is not CommandStatus.DELIVERED else 1,
                        command.created_at,
                    ),
                )
                if not candidates:
                    continue
                command = candidates[0]
                await self._apply_outcome_unknown(
                    state,
                    RecoveryDecision(
                        kind=RecoveryDecisionKind.OUTCOME_UNKNOWN,
                        action=RecoveryAction.OUTCOME_UNKNOWN,
                        reason_code=RecoveryReasonCode.DELIVERY_AMBIGUOUS,
                        observed_delivery_phase=(
                            ObservedDeliveryPhase.DELIVERED
                            if command.status is CommandStatus.DELIVERED
                            else ObservedDeliveryPhase.DELIVERY_STARTED
                        ),
                        command_id=command.id,
                        turn_id=(
                            state.active_turn.id
                            if state.active_turn is not None
                            and state.active_turn.command_id == command.id
                            else command.target_turn_id
                        ),
                    ),
                    fence=fence,
                    attempt_id=None,
                    trigger=RecoveryTrigger.SHUTDOWN,
                )
            except DomainError as exc:
                if exc.code is ErrorCode.STALE_OWNER:
                    await self._on_lost_lease(conversation_id)
                else:
                    logger.warning("shutdown_terminalization_failed code=%s", exc.code.value)
            except Exception:
                logger.warning(
                    "shutdown_terminalization_failed code=%s",
                    ErrorCode.INVALID_STATE.value,
                )

    async def finish_shutdown(self) -> None:
        """Stop heartbeat and release idle conversation/worker leases when clean."""
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._heartbeat_healthy = False

        live = getattr(self._runtime, "_runtimes", {})
        cleanup_finished = len(live) == 0

        if cleanup_finished and self._worker_id is not None:
            for conversation_id, fence in list(self._fences.items()):
                with contextlib.suppress(Exception):
                    await self._persistence.release_conversation_lease(
                        conversation_id,
                        self._worker_id,
                        fence,
                    )
            self._fences.clear()
            self._attempt_ids.clear()
            with contextlib.suppress(Exception):
                await self._persistence.release_worker_lease(self._worker_id)
            self._lease_healthy = False

    async def _heartbeat_loop(self) -> None:
        assert self._worker_id is not None
        interval = self._policy.lease_renewal_interval
        while True:
            try:
                await self._persistence.renew_worker_lease(
                    self._worker_id,
                    lease_duration=self._policy.lease_duration,
                )
                self._lease_healthy = True
                self._heartbeat_healthy = True
                lost = await self._persistence.renew_owned_conversation_leases(
                    self._worker_id,
                    lease_duration=self._policy.lease_duration,
                )
                for item in lost:
                    await self._on_lost_lease(item.conversation_id)
                if not self._draining and self._lease_healthy:
                    claimed = await self._recover_batch(trigger=RecoveryTrigger.TAKEOVER)
                    if not claimed and self._capacity_remaining() > 0:
                        self._initial_recovery_complete = True
            except DomainError as exc:
                if exc.code is ErrorCode.WORKER_LEASE_UNAVAILABLE:
                    await self._on_worker_lease_lost()
                    return
                logger.warning("worker heartbeat failed code=%s", exc.code.value)
                self._heartbeat_healthy = False
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worker heartbeat failed")
                self._heartbeat_healthy = False
            await asyncio.sleep(interval)

    async def _recover_batch(self, *, trigger: RecoveryTrigger) -> int:
        assert self._worker_id is not None
        remaining = self._capacity_remaining()
        if remaining <= 0:
            return 0
        ownerships = await self._persistence.claim_expired_conversations(
            self._worker_id,
            remaining,
            lease_duration=self._policy.lease_duration,
            trigger=trigger.value,
        )
        for ownership in ownerships:
            self._remember_ownership(ownership)
            await self._recover_owned(
                ownership.conversation_id,
                ownership.fence,
                attempt_id=ownership.recovery_attempt_id,
                trigger=trigger,
            )
        return len(ownerships)

    def _remember_ownership(self, ownership: ConversationOwnership) -> None:
        self._fences[ownership.conversation_id] = ownership.fence
        self._processor.set_fence(ownership.conversation_id, ownership.fence)
        if ownership.recovery_attempt_id is not None:
            self._attempt_ids[ownership.conversation_id] = ownership.recovery_attempt_id

    def _capacity_remaining(self) -> int:
        live = getattr(self._runtime, "_runtimes", {})
        candidates = getattr(self._runtime, "_candidates", {})
        used = len(live) + len(candidates)
        return max(0, self._policy.max_runtimes - used)

    async def _recover_owned(
        self,
        conversation_id: UUID,
        fence: int,
        *,
        attempt_id: UUID | None,
        trigger: RecoveryTrigger,
    ) -> None:
        assert self._worker_id is not None
        obs = get_observability()
        with obs.start_span(
            SPAN_WORKER_RECOVERY,
            recovery_trigger=trigger,
            database_system=self._database_system,
            operation="recover_owned",
        ) as span:
            try:
                state = await self._persistence.get_worker_snapshot(conversation_id)
                supports_resume = await self._probe_supports_resume(state)
                decisions = classify_conversation(
                    state,
                    now=self._clock(),
                    supports_resume=supports_resume,
                )
                for decision in decisions[:1]:
                    state = await self._persistence.get_worker_snapshot(conversation_id)
                    await self._apply_decision(
                        state,
                        decision,
                        fence=fence,
                        attempt_id=attempt_id,
                        trigger=trigger,
                    )
            except Exception:
                logger.error(
                    "recovery_failed code=%s",
                    RecoveryReasonCode.WORKER_LOST.value,
                )
                obs.mark_span_error(span, ErrorCode.INVALID_STATE)
                obs.record_recovery(
                    trigger=trigger,
                    action=RecoveryAction.INVARIANT_FAILURE,
                    outcome=RecoveryResultCode.FAILED.value,
                    error_code=ErrorCode.INVALID_STATE,
                )
                if attempt_id is not None:
                    with contextlib.suppress(Exception):
                        await self._persistence.complete_recovery_attempt(
                            attempt_id,
                            result=RecoveryResultCode.FAILED.value,
                            reason_code=RecoveryReasonCode.WORKER_LOST.value,
                            completed_at=self._clock(),
                        )
                with contextlib.suppress(Exception):
                    await self._runtime.close(conversation_id, reason="recovery_failed")
                self._attempt_ids.pop(conversation_id, None)

    async def _probe_supports_resume(self, state: ConversationState) -> bool:
        if state.binding is None:
            return False
        try:
            launch = await self._runtime.prepare_launch_snapshot(state.binding.configuration)
        except Exception:
            return False
        return bool(launch.capabilities.supports_resume)

    async def _apply_decision(
        self,
        state: ConversationState,
        decision: RecoveryDecision,
        *,
        fence: int,
        attempt_id: UUID | None,
        trigger: RecoveryTrigger,
    ) -> None:
        assert self._worker_id is not None
        kind = decision.kind
        if attempt_id is not None:
            await self._persistence.update_recovery_attempt(
                attempt_id,
                command_id=decision.command_id,
                turn_id=decision.turn_id,
                trigger=trigger.value,
                observed_delivery_phase=decision.observed_delivery_phase.value,
                action=decision.action.value,
                reason_code=decision.reason_code.value,
                worker_id=self._worker_id,
                fence=fence,
            )
        if kind in {
            RecoveryDecisionKind.LEAVE_CLAIMABLE,
            RecoveryDecisionKind.RECLAIM,
            RecoveryDecisionKind.NO_ACTION,
        }:
            result = (
                RecoveryResultCode.SUCCESS.value
                if kind is RecoveryDecisionKind.RECLAIM
                else RecoveryResultCode.NO_ACTION.value
            )
            await self._complete_attempt(
                attempt_id,
                result=result,
                reason_code=decision.reason_code.value,
                trigger=trigger,
                action=decision.action,
            )
            return

        if kind in {
            RecoveryDecisionKind.OUTCOME_UNKNOWN,
            RecoveryDecisionKind.INVARIANT_FAILURE,
        }:
            await self._apply_outcome_unknown(
                state,
                decision,
                fence=fence,
                attempt_id=attempt_id,
                trigger=trigger,
            )
            return

        if kind is RecoveryDecisionKind.NATIVE_RESUME:
            ok = await self._apply_native_resume(
                state,
                decision,
                fence=fence,
                attempt_id=attempt_id,
                trigger=trigger,
            )
            if ok:
                return
            # Fall through to handoff fallback.
            state = await self._persistence.get_worker_snapshot(state.conversation.id)
            await self._apply_handoff_fallback(
                state,
                decision,
                fence=fence,
                attempt_id=attempt_id,
                trigger=trigger,
            )
            return

        if kind is RecoveryDecisionKind.HANDOFF_FALLBACK:
            await self._apply_handoff_fallback(
                state,
                decision,
                fence=fence,
                attempt_id=attempt_id,
                trigger=trigger,
            )
            return

    async def _apply_outcome_unknown(
        self,
        state: ConversationState,
        decision: RecoveryDecision,
        *,
        fence: int,
        attempt_id: UUID | None,
        trigger: RecoveryTrigger,
    ) -> None:
        assert self._worker_id is not None
        now = self._clock()
        command = (
            state.commands.get(decision.command_id) if decision.command_id is not None else None
        )
        reason = decision.reason_code
        events = ()
        commands: list[Command] = []
        next_state = state
        interrupted_turn_id: UUID | None = None

        if is_switch_command(command):
            assert command is not None
            failed = fail_switch(
                state,
                now=now,
                message=reason.value,
                error_code=reason.value,
            )
            marked = command.model_copy(
                update={
                    "status": CommandStatus.OUTCOME_UNKNOWN,
                    "recovery_attempt_id": attempt_id,
                    "worker_id": None,
                    "lease_expires_at": None,
                }
            )
            cmds = dict(failed.state.commands)
            cmds[marked.id] = marked
            next_state = failed.state.model_copy(update={"commands": cmds})
            events = failed.events
            commands = [marked]
        else:
            if state.active_turn is not None:
                ou = mark_outcome_unknown(
                    state,
                    now=now,
                    delivery_phase=decision.observed_delivery_phase.value,
                    message=reason.value,
                )
                next_state = ou.state
                events = ou.events
            activities = fail_running_activities(next_state, now=now, summary="worker_lost")
            next_state = activities.state
            events = tuple(events) + activities.events

            if command is not None:
                marked = next_state.commands.get(command.id, command).model_copy(
                    update={
                        "status": CommandStatus.OUTCOME_UNKNOWN,
                        "recovery_attempt_id": attempt_id,
                        "worker_id": None,
                        "lease_expires_at": None,
                    }
                )
                cmds = dict(next_state.commands)
                cmds[marked.id] = marked
                next_state = next_state.model_copy(update={"commands": cmds})
                commands.append(marked)

            # A conversation-level ambiguous transition fences every delivery
            # whose native outcome is unknown, including the delivered root.
            for current in next_state.commands.values():
                if (
                    current.status is CommandStatus.DELIVERY_STARTED
                    or (
                        current.status is CommandStatus.CLAIMED
                        and current.delivery_started_at is not None
                    )
                ) and current.id not in {item.id for item in commands}:
                    marked = current.model_copy(
                        update={
                            "status": CommandStatus.OUTCOME_UNKNOWN,
                            "recovery_attempt_id": attempt_id,
                            "worker_id": None,
                            "lease_expires_at": None,
                        }
                    )
                    commands.append(marked)
                    updated_commands = dict(next_state.commands)
                    updated_commands[marked.id] = marked
                    next_state = next_state.model_copy(update={"commands": updated_commands})

            # Collect any other commands flipped by mark_outcome_unknown.
            for cmd in next_state.commands.values():
                if cmd.status is CommandStatus.OUTCOME_UNKNOWN and cmd.id not in {
                    c.id for c in commands
                }:
                    commands.append(
                        cmd.model_copy(
                            update={
                                "recovery_attempt_id": attempt_id,
                                "worker_id": None,
                                "lease_expires_at": None,
                            }
                        )
                    )

            interrupted_turn_id = (
                decision.turn_id
                if (
                    turn_needs_interrupt_messages(state, decision.turn_id)
                    and decision.turn_id is not None
                )
                else None
            )

        committed = await self._persistence.commit_recovery_batch(
            state.conversation.id,
            state.conversation.version,
            next_state,
            events,
            tuple(commands),
            interrupted_turn_id=interrupted_turn_id,
            attempt_id=attempt_id,
            command_id=decision.command_id,
            turn_id=decision.turn_id,
            trigger=trigger.value,
            observed_delivery_phase=decision.observed_delivery_phase.value,
            action=decision.action.value,
            result=RecoveryResultCode.SUCCESS.value,
            reason_code=reason.value,
            completed_at=now,
            worker_id=self._worker_id,
            fence=fence,
        )

        await self._safe_publish(committed, state=next_state)
        get_observability().record_recovery(
            trigger=trigger,
            action=decision.action,
            outcome=RecoveryResultCode.SUCCESS.value,
        )

    async def _apply_native_resume(
        self,
        state: ConversationState,
        decision: RecoveryDecision,
        *,
        fence: int,
        attempt_id: UUID | None,
        trigger: RecoveryTrigger,
    ) -> bool:
        assert self._worker_id is not None
        binding = state.binding
        if binding is None or not binding.native_session_id:
            return False
        try:
            managed, reason = await self._runtime.resume_for_recovery(
                state.conversation.id,
                state.conversation.owner_id,
                binding.configuration,
                binding.native_session_id,
                worker_id=self._worker_id,
                fence=fence,
                expected_binding_kind=binding.kind,
                previous_launch=binding.launch_snapshot,
            )
        except DomainError as exc:
            if exc.code is ErrorCode.STALE_OWNER:
                await self._on_lost_lease(state.conversation.id)
                return True
            logger.warning(
                "native_resume_failed conversation=%s code=%s",
                state.conversation.id,
                RecoveryReasonCode.RESUME_REJECTED.value,
            )
            return False
        except Exception:
            logger.warning(
                "native_resume_failed conversation=%s code=%s",
                state.conversation.id,
                RecoveryReasonCode.RESUME_REJECTED.value,
            )
            return False

        self._processor.set_fence(state.conversation.id, fence)
        self._processor.ensure_pump(state.conversation.id)
        _ = managed
        await self._complete_attempt(
            attempt_id,
            result=RecoveryResultCode.SUCCESS.value,
            reason_code=reason.value,
            trigger=trigger,
            action=decision.action,
        )
        return True

    async def _apply_handoff_fallback(
        self,
        state: ConversationState,
        decision: RecoveryDecision,
        *,
        fence: int,
        attempt_id: UUID | None,
        trigger: RecoveryTrigger,
    ) -> None:
        assert self._worker_id is not None
        # Terminalize unobservable work first when still live.
        if state.active_turn is not None or any(
            a.status is ActivityStatus.RUNNING for a in state.activities.values()
        ):
            await self._apply_outcome_unknown(
                state,
                RecoveryDecision(
                    kind=RecoveryDecisionKind.OUTCOME_UNKNOWN,
                    action=RecoveryAction.OUTCOME_UNKNOWN,
                    reason_code=RecoveryReasonCode.RECOVERY_FALLBACK,
                    observed_delivery_phase=decision.observed_delivery_phase,
                    command_id=decision.command_id,
                    turn_id=decision.turn_id,
                ),
                fence=fence,
                attempt_id=None,  # complete after fallback
                trigger=trigger,
            )
            state = await self._persistence.get_worker_snapshot(state.conversation.id)

        binding = state.binding
        if binding is None:
            await self._complete_attempt(
                attempt_id,
                result=RecoveryResultCode.FAILED.value,
                reason_code=RecoveryReasonCode.RECOVERY_FALLBACK.value,
                trigger=trigger,
                action=RecoveryAction.HANDOFF_FALLBACK,
            )
            return

        handoff = await self._persistence.read_retained_handoff(state.conversation.id)
        handoff_text = render_handoff(handoff)
        managed = await self._runtime.recovery_handoff_fallback(
            state.conversation.id,
            state.conversation.owner_id,
            binding.id,
            binding.configuration,
            handoff_text,
            worker_id=self._worker_id,
            fence=fence,
        )
        if managed is None:
            await self._complete_attempt(
                attempt_id,
                result=RecoveryResultCode.FAILED.value,
                reason_code=RecoveryReasonCode.RECOVERY_FALLBACK.value,
                trigger=trigger,
                action=RecoveryAction.HANDOFF_FALLBACK,
            )
            return

        # Commit session rotation under the fence, then promote when observation needed.
        state = await self._persistence.get_worker_snapshot(state.conversation.id)
        await self._persistence.commit_session_rotation(
            state.conversation.id,
            state.conversation.version,
            native_session_id=managed.session.native_session_id,
            launch_snapshot=managed.launch,
            worker_id=self._worker_id,
            fence=fence,
        )
        await checkpoint(self._fault_callback, FaultPoint.AFTER_SESSION_ROTATION_COMMIT)
        needs_observation = (
            state.active_turn is not None
            or any(a.status is ActivityStatus.RUNNING for a in state.activities.values())
            or state.conversation.status.value in {"running", "waiting", "background_active"}
        )
        # After terminalization status may be idle; still promote if caller needed observation.
        # For idle recovery, close candidate while retaining durable resume identity.
        if needs_observation:
            await self._runtime.promote_candidate(state.conversation.id, binding.id)
            self._processor.set_fence(state.conversation.id, fence)
            self._processor.ensure_pump(state.conversation.id)
        else:
            await self._runtime.close_candidate(binding.id)
        await self._complete_attempt(
            attempt_id,
            result=RecoveryResultCode.SUCCESS.value,
            reason_code=RecoveryReasonCode.RECOVERY_FALLBACK.value,
            trigger=trigger,
            action=RecoveryAction.HANDOFF_FALLBACK,
        )

    async def _complete_attempt(
        self,
        attempt_id: UUID | None,
        *,
        result: str,
        reason_code: str,
        trigger: RecoveryTrigger,
        action: RecoveryAction,
    ) -> None:
        get_observability().record_recovery(
            trigger=trigger,
            action=action,
            outcome=result,
        )
        if attempt_id is None:
            return
        with contextlib.suppress(Exception):
            await self._persistence.complete_recovery_attempt(
                attempt_id,
                result=result,
                reason_code=reason_code,
                completed_at=self._clock(),
            )

    async def _release_undelivered_claims(self) -> None:
        if self._worker_id is None:
            return
        for conversation_id, fence in list(self._fences.items()):
            try:
                state = await self._persistence.get_worker_snapshot(conversation_id)
            except Exception:
                continue
            released: list[Command] = []
            commands = dict(state.commands)
            for command in state.commands.values():
                if (
                    command.status is CommandStatus.CLAIMED
                    and command.worker_id == self._worker_id
                    and command.delivery_started_at is None
                ):
                    updated = command.model_copy(
                        update={
                            "status": CommandStatus.ACCEPTED,
                            "worker_id": None,
                            "lease_expires_at": None,
                        }
                    )
                    commands[updated.id] = updated
                    released.append(updated)
            if not released:
                continue
            with contextlib.suppress(Exception):
                await self._persistence.commit_turn_batch(
                    conversation_id,
                    state.conversation.version,
                    state.model_copy(update={"commands": commands}),
                    (),
                    tuple(released),
                    worker_id=self._worker_id,
                    fence=fence,
                )

    async def _on_lost_lease(self, conversation_id: UUID) -> None:
        self._fences.pop(conversation_id, None)
        self._attempt_ids.pop(conversation_id, None)
        self._processor.drop_fence(conversation_id)
        with contextlib.suppress(Exception):
            await self._processor.cancel_pump(conversation_id)
        with contextlib.suppress(Exception):
            await self._runtime.close(conversation_id, reason="lease_lost")

    async def _on_worker_lease_lost(self) -> None:
        self._lease_healthy = False
        self._heartbeat_healthy = False
        self._claims_healthy = False
        self._processor.set_claims_enabled(False)
        for conversation_id in list(self._fences):
            await self._on_lost_lease(conversation_id)
        with contextlib.suppress(Exception):
            await self._runtime.shutdown(deadline=time.monotonic() + self._policy.shutdown_budget)

    async def _safe_publish(
        self,
        events: object,
        *,
        state: ConversationState | None = None,
    ) -> None:
        if not events:
            return
        get_observability().observe_committed_events(events, state=state)  # type: ignore[arg-type]
        try:
            await self._publisher.publish(events)  # type: ignore[arg-type]
        except Exception:
            logger.exception("publisher failed after recovery commit")
