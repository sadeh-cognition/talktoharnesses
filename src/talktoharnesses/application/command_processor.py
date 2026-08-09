"""Provider-neutral durable command worker (explicit start/stop)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol, TypedDict, runtime_checkable
from uuid import UUID, uuid4

from talktoharnesses.application.delta_batcher import DeltaBatcher
from talktoharnesses.application.event_dispatcher import (
    apply_outcome_unknown,
    dispatch_harness_event,
    mark_command_delivered,
    mark_command_delivery_started,
)
from talktoharnesses.application.faults import FaultCallback, FaultPoint, checkpoint
from talktoharnesses.application.handoff import render_handoff
from talktoharnesses.application.observability import get_observability
from talktoharnesses.application.persistence import Persistence
from talktoharnesses.application.publisher import CommittedEventPublisher
from talktoharnesses.domain.enums import ActivityStatus, CommandKind, CommandStatus, ErrorCode
from talktoharnesses.domain.errors import DomainError, public_message
from talktoharnesses.domain.events import (
    ConversationEvent,
    HarnessEvent,
    InteractionRequestedPayload,
)
from talktoharnesses.domain.models import (
    AnswerInteractionPayload,
    Command,
    ConversationHarnessBinding,
    InteractionAnswer,
    PendingInteraction,
    SteerPayload,
    SubmitTurnPayload,
    SwitchHarnessPayload,
)
from talktoharnesses.domain.transitions import (
    ConversationState,
    apply_steer,
    commit_switch,
    fail_switch,
    start_turn,
)
from talktoharnesses.providers.adapter import (
    HarnessAdapter,
    HarnessInteractionRequest,
    HarnessSession,
    SteerRequest,
    TurnRequest,
)
from talktoharnesses.runtime.manager import ManagedRuntime, RuntimeManager

logger = logging.getLogger(__name__)


class _FenceCommitKwargs(TypedDict, total=False):
    worker_id: str
    fence: int


@runtime_checkable
class _NativeDedupeAdapter(Protocol):
    def import_seen(
        self,
        native_ids: frozenset[str],
        stream_offsets: frozenset[str],
    ) -> None: ...

    def export_seen(self) -> tuple[frozenset[str], frozenset[str]]: ...


class CommandProcessor:
    """Claim loop + per-conversation delivery and event pump.

    Owner must call :meth:`start` / :meth:`stop` explicitly. Never started from
    Django ``AppConfig.ready()``.
    """

    def __init__(
        self,
        persistence: Persistence,
        publisher: CommittedEventPublisher,
        runtime_manager: RuntimeManager,
        *,
        claim_limit: int = 8,
        lease_seconds: float = 30.0,
        poll_interval: float = 0.05,
        clock: Callable[[], datetime] | None = None,
        interaction_broker: object | None = None,
        fault_callback: FaultCallback = None,
    ) -> None:
        self._persistence = persistence
        self._publisher = publisher
        self._runtime = runtime_manager
        self._claim_limit = claim_limit
        self._lease_seconds = lease_seconds
        self._poll_interval = poll_interval
        self._clock = clock or (lambda: datetime.now(UTC))
        self._broker = interaction_broker
        self._fault_callback = fault_callback

        self._worker_id: str | None = None
        self._running = False
        self._draining = False
        # Coordinator disables claims until initial recovery finishes.
        self._claims_enabled = True
        self._fences: dict[UUID, int] = {}
        self._claim_task: asyncio.Task[None] | None = None
        self._command_tasks: set[asyncio.Task[None]] = set()
        self._conv_locks: dict[UUID, asyncio.Lock] = {}
        self._pumps: dict[UUID, asyncio.Task[None]] = {}
        self._batchers: dict[UUID, DeltaBatcher] = {}

    async def start(self, worker_id: str) -> None:
        if self._running:
            return
        self.initialize_worker(worker_id)
        self._running = True
        self._draining = False
        self._claim_task = asyncio.create_task(self._claim_loop(), name=f"claim-{worker_id}")

    def initialize_worker(self, worker_id: str) -> None:
        """Install ownership before recovery can create event pumps."""
        if self._running and self._worker_id != worker_id:
            raise DomainError(ErrorCode.INVALID_STATE, "command worker already started")
        self._worker_id = worker_id

    @property
    def claim_loop_healthy(self) -> bool:
        task = self._claim_task
        return self._running and task is not None and not task.done()

    async def stop(self) -> None:
        self._running = False
        self._claims_enabled = False
        if self._claim_task is not None:
            self._claim_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._claim_task
            self._claim_task = None
        for task in list(self._command_tasks):
            task.cancel()
        if self._command_tasks:
            await asyncio.gather(*self._command_tasks, return_exceptions=True)
        self._command_tasks.clear()
        for task in list(self._pumps.values()):
            task.cancel()
        if self._pumps:
            await asyncio.gather(*self._pumps.values(), return_exceptions=True)
        self._pumps.clear()
        for batcher in list(self._batchers.values()):
            with contextlib.suppress(Exception):
                await batcher.close()
        self._batchers.clear()

    def set_claims_enabled(self, enabled: bool) -> None:
        self._claims_enabled = enabled

    def begin_shutdown(self) -> None:
        self._draining = True
        self._claims_enabled = False

    def set_fence(self, conversation_id: UUID, fence: int) -> None:
        self._fences[conversation_id] = fence

    def drop_fence(self, conversation_id: UUID) -> None:
        self._fences.pop(conversation_id, None)

    def ensure_pump(self, conversation_id: UUID) -> None:
        self._ensure_pump(conversation_id)

    async def cancel_pump(self, conversation_id: UUID) -> None:
        await self._quiesce_pump(conversation_id)

    def _fence_kwargs(self, conversation_id: UUID) -> _FenceCommitKwargs:
        fence = self._fences.get(conversation_id)
        if self._worker_id is None or fence is None:
            return {}
        return {"worker_id": self._worker_id, "fence": fence}

    def _lock_for(self, conversation_id: UUID) -> asyncio.Lock:
        lock = self._conv_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._conv_locks[conversation_id] = lock
        return lock

    async def _claim_loop(self) -> None:
        assert self._worker_id is not None
        while self._running:
            try:
                if self._claims_enabled:
                    claimed = await self._persistence.claim_commands(
                        self._worker_id,
                        self._claim_limit,
                        lease_duration=self._lease_seconds,
                    )
                    for claimed_command in claimed:
                        command = claimed_command.command
                        self.set_fence(command.conversation_id, claimed_command.fence)
                        await checkpoint(self._fault_callback, FaultPoint.AFTER_CLAIM_COMMIT)
                        task = asyncio.create_task(
                            self._handle_command(command),
                            name=f"cmd-{command.id}",
                        )
                        self._command_tasks.add(task)
                        task.add_done_callback(self._command_tasks.discard)
            except Exception:
                logger.exception("command claim failed")
            await asyncio.sleep(self._poll_interval)

    async def _handle_command(self, command: Command) -> None:
        async with self._lock_for(command.conversation_id):
            try:
                await self._execute_command(command)
            except DomainError as exc:
                if exc.code is ErrorCode.STALE_OWNER:
                    await self._on_stale_owner(command.conversation_id)
                    return
                logger.exception(
                    "command execution failed conversation=%s command=%s",
                    command.conversation_id,
                    command.id,
                )
            except Exception:
                logger.exception(
                    "command execution failed conversation=%s command=%s",
                    command.conversation_id,
                    command.id,
                )

    async def _on_stale_owner(self, conversation_id: UUID) -> None:
        await self._quiesce_pump(conversation_id)
        with contextlib.suppress(Exception):
            await self._runtime.close(conversation_id, reason="stale_owner")
        self.drop_fence(conversation_id)

    async def _execute_command(self, command: Command) -> None:
        if self._draining:
            return
        state = await self._persistence.get_worker_snapshot(command.conversation_id)
        now = self._clock()

        # The aggregate command projection predates this worker's durable claim.
        # Carry the claimed row forward so later projection writes retain its
        # owner, lease, and attempt metadata.
        commands = dict(state.commands)
        commands[command.id] = command
        state = state.model_copy(update={"commands": commands})

        # Switching replaces the binding, so it must never resume the old one first.
        if command.kind == CommandKind.SWITCH_HARNESS:
            await self._execute_switch(command, state)
            return

        if await self._runtime.ensure_binding_current(command.conversation_id, state) is None:
            await self._ensure_runtime(state)
            state = await self._persistence.get_worker_snapshot(command.conversation_id)
            commands = dict(state.commands)
            commands[command.id] = command
            state = state.model_copy(update={"commands": commands})

        managed = self._runtime.get_runtime(command.conversation_id)
        if managed is None:
            raise DomainError(ErrorCode.INVALID_STATE, "failed to obtain runtime")

        # Queued next-turn SUBMIT must wait until the active turn finishes.
        if (
            command.kind == CommandKind.SUBMIT_TURN
            and state.active_turn is not None
            and state.active_turn.command_id != command.id
        ):
            logger.info(
                "deferring submit until active turn finishes conversation=%s command=%s",
                command.conversation_id,
                command.id,
            )
            await self._persistence.commit_turn_batch(
                command.conversation_id,
                state.conversation.version,
                state,
                (),
                (command,),
                **self._fence_kwargs(command.conversation_id),
            )
            await self._renew_lease(command)
            return

        queued_prompt: str | None = None
        if (
            command.kind == CommandKind.SUBMIT_TURN
            and state.active_turn is None
            and state.queued_turn is not None
        ):
            queued_prompt = state.queued_user_text
            result = start_turn(state, now=now)
            committed = await self._persistence.commit_turn_batch(
                command.conversation_id,
                state.conversation.version,
                result.state,
                result.events,
                tuple(result.state.commands.values()),
                **self._fence_kwargs(command.conversation_id),
            )
            await self._safe_publish(committed, state=result.state)
            state = await self._persistence.get_worker_snapshot(command.conversation_id)

        now = self._clock()
        state, started_cmd = mark_command_delivery_started(state, command.id, now=now)
        await self._persistence.update_command(
            started_cmd,
            **self._fence_kwargs(command.conversation_id),
        )
        await checkpoint(self._fault_callback, FaultPoint.AFTER_DELIVERY_STARTED)
        if self._draining:
            return

        adapter = managed.adapter
        session = managed.session

        if command.kind == CommandKind.ANSWER_INTERACTION:
            await self._execute_answer_delivery(started_cmd, state, adapter, session)
            self._ensure_pump(command.conversation_id)
            return

        await self._renew_lease(started_cmd)

        if command.kind == CommandKind.SUBMIT_TURN:
            assert isinstance(command.payload, SubmitTurnPayload)
            turn_id = (
                state.active_turn.id if state.active_turn is not None else command.target_turn_id
            )
            if turn_id is None:
                raise DomainError(ErrorCode.NO_ACTIVE_TURN, "submit has no target turn")
            await adapter.submit(
                session,
                TurnRequest(
                    turn_id=turn_id,
                    command_id=command.id,
                    prompt=queued_prompt or command.payload.prompt,
                    model=command.payload.model,
                ),
            )
        elif command.kind == CommandKind.STEER:
            assert isinstance(command.payload, SteerPayload)
            turn_id = (
                state.active_turn.id if state.active_turn is not None else command.target_turn_id
            )
            if turn_id is None:
                raise DomainError(ErrorCode.NO_ACTIVE_TURN, "steer has no target turn")
            ok = await adapter.steer(
                session,
                SteerRequest(
                    turn_id=turn_id,
                    command_id=command.id,
                    prompt=command.payload.prompt,
                ),
            )
            if not ok:
                logger.info("steer unsupported for command %s; queueing prompt", command.id)
                await self._fallback_failed_steer(started_cmd)
                return
        elif command.kind == CommandKind.INTERRUPT:
            batcher = self._batchers.get(command.conversation_id)
            if batcher is not None:
                await batcher.flush()
            if self._broker is not None:
                await self._broker.cancel_open_for_interrupt(  # type: ignore[attr-defined]
                    command.conversation_id,
                    **self._fence_kwargs(command.conversation_id),
                )
            await adapter.interrupt(session)
        else:
            logger.warning("command kind %s not executable by worker", command.kind)
            settled = started_cmd.model_copy(
                update={
                    "status": CommandStatus.SETTLED,
                    "settled_at": self._clock(),
                    "worker_id": None,
                    "lease_expires_at": None,
                }
            )
            await self._persistence.update_command(
                settled,
                **self._fence_kwargs(command.conversation_id),
            )
            return

        await checkpoint(self._fault_callback, FaultPoint.AFTER_NATIVE_ACK)

        if self._draining:
            return

        now = self._clock()
        state = await self._persistence.get_worker_snapshot(command.conversation_id)
        if command.id in state.commands:
            _, delivered_cmd = mark_command_delivered(state, command.id, now=now)
        else:
            delivered_cmd = started_cmd.model_copy(
                update={"status": CommandStatus.DELIVERED, "delivered_at": now}
            )
        await self._persistence.update_command(
            delivered_cmd,
            **self._fence_kwargs(command.conversation_id),
        )
        await checkpoint(self._fault_callback, FaultPoint.AFTER_DELIVERED)
        get_observability().record_command(
            kind=command.kind,
            outcome=CommandStatus.DELIVERED.value,
        )

        self._ensure_pump(command.conversation_id)

    async def _execute_answer_delivery(
        self,
        command: Command,
        state: ConversationState,
        adapter: HarnessAdapter,
        session: HarnessSession,
    ) -> None:
        assert isinstance(command.payload, AnswerInteractionPayload)
        try:
            await self._renew_lease(command)
            answer = state.answers.get(command.payload.interaction_id)
            if answer is None:
                answer = InteractionAnswer(
                    interaction_id=command.payload.interaction_id,
                    submitted_at=self._clock(),
                )
            await adapter.answer_interaction(session, answer)
            await checkpoint(self._fault_callback, FaultPoint.AFTER_NATIVE_ACK)

            if self._draining:
                return

            now = self._clock()
            latest = await self._persistence.get_worker_snapshot(command.conversation_id)
            if command.id in latest.commands:
                _, delivered = mark_command_delivered(latest, command.id, now=now)
            else:
                delivered = command.model_copy(
                    update={"status": CommandStatus.DELIVERED, "delivered_at": now}
                )
            settled = delivered.model_copy(
                update={
                    "status": CommandStatus.SETTLED,
                    "settled_at": now,
                    "worker_id": None,
                    "lease_expires_at": None,
                }
            )
            commands = dict(latest.commands)
            commands[settled.id] = settled
            settled_state = latest.model_copy(update={"commands": commands})
            await self._persistence.commit_turn_batch(
                command.conversation_id,
                latest.conversation.version,
                settled_state,
                (),
                (settled,),
                **self._fence_kwargs(command.conversation_id),
            )
            await checkpoint(self._fault_callback, FaultPoint.AFTER_DELIVERED)
        except BaseException as exc:
            await asyncio.shield(self._mark_answer_outcome_unknown(command, exc))
            raise

    async def _mark_answer_outcome_unknown(
        self,
        command: Command,
        exc: BaseException,
    ) -> None:
        _ = exc
        state = await self._persistence.get_worker_snapshot(command.conversation_id)
        current = state.commands.get(command.id, command)
        if current.status in {CommandStatus.SETTLED, CommandStatus.OUTCOME_UNKNOWN}:
            return
        failed = current.model_copy(
            update={
                "status": CommandStatus.OUTCOME_UNKNOWN,
                "recovery_attempt_id": None,
                "worker_id": None,
                "lease_expires_at": None,
            }
        )
        commands = dict(state.commands)
        commands[failed.id] = failed
        failed_state = state.model_copy(update={"commands": commands})
        await self._persistence.commit_turn_batch(
            command.conversation_id,
            state.conversation.version,
            failed_state,
            (),
            (failed,),
            **self._fence_kwargs(command.conversation_id),
        )

    async def _fallback_failed_steer(self, command: Command) -> None:
        """Queue the steer prompt and release the command for later submit."""
        assert isinstance(command.payload, SteerPayload)
        state = await self._persistence.get_worker_snapshot(command.conversation_id)
        now = self._clock()
        current = state.commands.get(command.id, command)
        result = apply_steer(
            state,
            prompt=command.payload.prompt,
            idempotency_key=command.idempotency_key,
            now=now,
            command=current,
            steer_succeeded=False,
        )
        queued = result.command or current
        released = queued.model_copy(
            update={
                "status": CommandStatus.ACCEPTED,
                "worker_id": None,
                "lease_expires_at": None,
                "delivery_started_at": None,
            }
        )
        commands = dict(result.state.commands)
        commands[released.id] = released
        next_state = result.state.model_copy(update={"commands": commands})
        committed = await self._persistence.commit_turn_batch(
            command.conversation_id,
            state.conversation.version,
            next_state,
            result.events,
            (released,),
            **self._fence_kwargs(command.conversation_id),
        )
        await self._safe_publish(committed, state=next_state)

    async def _execute_switch(self, command: Command, _state: ConversationState) -> None:
        """Durable harness switch: candidate first, current binding until commit."""
        assert isinstance(command.payload, SwitchHarnessPayload)
        conversation_id = command.conversation_id
        prepared = await self._persistence.prepare_harness_switch(conversation_id)
        state = prepared.state
        commands = dict(state.commands)
        commands[command.id] = command
        state = state.model_copy(update={"commands": commands})
        if state.binding is None:
            raise DomainError(ErrorCode.INVALID_STATE, "conversation has no binding")
        if (
            state.active_turn is not None
            or state.queued_turn is not None
            or any(a.status is ActivityStatus.RUNNING for a in state.activities.values())
        ):
            logger.info(
                "deferring switch until conversation is idle conversation=%s command=%s",
                conversation_id,
                command.id,
            )
            await self._release_to_accepted(command, state)
            return

        switch_payload = command.payload
        configuration = switch_payload.configuration
        harness_instance_id = switch_payload.harness_instance_id
        binding_id = uuid4()
        quiesced = False
        lease_task: asyncio.Task[None] | None = None
        parent_task = asyncio.current_task()

        async def renew_lease() -> None:
            assert self._worker_id is not None
            while True:
                await self._persistence.renew_command_lease(
                    command.id,
                    self._worker_id,
                    lease_duration=self._lease_seconds,
                    fence=self._fences.get(conversation_id),
                )
                await asyncio.sleep(max(0.01, self._lease_seconds / 3))

        def stop_on_lost_lease(task: asyncio.Task[None]) -> None:
            if not task.cancelled() and task.exception() is not None and parent_task is not None:
                logger.warning("switch command lease renewal failed command=%s", command.id)
                parent_task.cancel()

        try:
            if self._worker_id is not None:
                lease_task = asyncio.create_task(
                    renew_lease(),
                    name=f"switch-lease-{command.id}",
                )
                lease_task.add_done_callback(stop_on_lost_lease)

            now = self._clock()
            state, started_cmd = mark_command_delivery_started(state, command.id, now=now)
            await self._persistence.update_command(
                started_cmd,
                **self._fence_kwargs(conversation_id),
            )
            await checkpoint(self._fault_callback, FaultPoint.AFTER_DELIVERY_STARTED)
            command = started_cmd
            if self._draining:
                return

            candidate = await self._runtime.start_candidate(
                conversation_id=conversation_id,
                owner_id=state.conversation.owner_id,
                binding_id=binding_id,
                configuration=configuration,
                **self._fence_kwargs(conversation_id),
            )
            await self._runtime.seed_candidate(candidate, render_handoff(prepared.handoff))
            await checkpoint(self._fault_callback, FaultPoint.AFTER_NATIVE_ACK)

            if self._draining:
                await self._runtime.close_candidate(binding_id)
                return

            now = self._clock()
            # Update the durable command row only — keep prepared aggregate
            # version so commit_harness_switch still observes OCC conflicts.
            state, delivered_cmd = mark_command_delivered(state, command.id, now=now)
            await self._persistence.update_command(
                delivered_cmd,
                **self._fence_kwargs(conversation_id),
            )
            await checkpoint(self._fault_callback, FaultPoint.AFTER_DELIVERED)
            command = delivered_cmd
            get_observability().record_command(
                kind=command.kind,
                outcome=CommandStatus.DELIVERED.value,
            )

            await self._quiesce_pump(conversation_id)
            quiesced = True

            if lease_task is not None:
                lease_task.cancel()
                await asyncio.gather(lease_task, return_exceptions=True)
                lease_task = None
                assert self._worker_id is not None
                await self._persistence.renew_command_lease(
                    command.id,
                    self._worker_id,
                    lease_duration=self._lease_seconds,
                    fence=self._fences.get(conversation_id),
                )

            now = self._clock()
            result = commit_switch(
                state,
                new_binding=ConversationHarnessBinding(
                    id=binding_id,
                    conversation_id=conversation_id,
                    kind=configuration.kind,
                    configuration=configuration,
                    harness_instance_id=harness_instance_id,
                    native_session_id=candidate.session.native_session_id,
                    launch_snapshot=candidate.launch,
                    created_at=now,
                ),
                now=now,
            )
            settled = self._settled(command, now=now)
            commands = dict(result.state.commands)
            commands[settled.id] = settled
            committed = await self._persistence.commit_harness_switch(
                conversation_id,
                state.conversation.version,
                result.state.model_copy(
                    update={
                        "commands": commands,
                        # The new native session starts with an empty dedupe set.
                        "seen_native_ids": frozenset(),
                        "seen_stream_offsets": frozenset(),
                    }
                ),
                result.events,
                command=settled,
                process=candidate.process_record,
                launch_history_entry=candidate.launch,
                **self._fence_kwargs(conversation_id),
            )
        except asyncio.CancelledError:
            # Closing the candidate is non-negotiable even during shutdown.
            await asyncio.shield(self._runtime.close_candidate(binding_id))
            if quiesced and self._running:
                self._ensure_pump(conversation_id)
            raise
        except Exception as exc:
            if lease_task is not None:
                lease_task.cancel()
                await asyncio.gather(lease_task, return_exceptions=True)
                lease_task = None
            await self._runtime.close_candidate(binding_id)
            if quiesced:
                self._ensure_pump(conversation_id)
            await self._fail_switch(command, exc)
            return
        finally:
            if lease_task is not None:
                lease_task.cancel()
                await asyncio.gather(lease_task, return_exceptions=True)

        await self._safe_publish(committed, state=result.state)
        previous = self._runtime.get_runtime(conversation_id)
        await self._runtime.promote_candidate(conversation_id, binding_id)
        if previous is not None:
            try:
                await self._runtime.close_replaced_runtime(previous)
            except Exception:
                logger.exception(
                    "failed to close replaced runtime conversation=%s",
                    conversation_id,
                )
        self._ensure_pump(conversation_id)

    async def _fail_switch(self, command: Command, exc: BaseException) -> None:
        """Settle the switch command and publish only harness_switch_failed."""
        err = exc.code if isinstance(exc, DomainError) else ErrorCode.INVALID_STATE
        logger.warning(
            "harness switch failed code=%s",
            err.value,
        )
        code = err.value
        message = public_message(err)
        state = await self._persistence.get_worker_snapshot(command.conversation_id)
        now = self._clock()
        result = fail_switch(state, now=now, message=message, error_code=code)
        get_observability().record_command(kind=command.kind, outcome="failed")
        settled = self._settled(
            state.commands.get(command.id, command),
            now=now,
        )
        commands = dict(result.state.commands)
        commands[settled.id] = settled
        committed = await self._persistence.commit_harness_switch_failure(
            command.conversation_id,
            state.conversation.version,
            result.state.model_copy(update={"commands": commands}),
            result.events,
            command=settled,
            **self._fence_kwargs(command.conversation_id),
        )
        await self._safe_publish(committed, state=result.state)

    def _settled(self, command: Command, *, now: datetime) -> Command:
        return command.model_copy(
            update={
                "status": CommandStatus.SETTLED,
                "delivered_at": command.delivered_at or now,
                "settled_at": now,
                "worker_id": None,
                "lease_expires_at": None,
            }
        )

    async def _release_to_accepted(self, command: Command, state: ConversationState) -> None:
        """Return a claimed command to the accepted pool without executing it."""
        released = command.model_copy(
            update={
                "status": CommandStatus.ACCEPTED,
                "worker_id": None,
                "lease_expires_at": None,
                "delivery_started_at": None,
            }
        )
        commands = dict(state.commands)
        commands[released.id] = released
        await self._persistence.commit_turn_batch(
            command.conversation_id,
            state.conversation.version,
            state.model_copy(update={"commands": commands}),
            (),
            (released,),
            **self._fence_kwargs(command.conversation_id),
        )

    async def _quiesce_pump(self, conversation_id: UUID) -> None:
        """Stop the current event pump and flush its pending delta batch."""
        pump = self._pumps.pop(conversation_id, None)
        if pump is not None and not pump.done():
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump
        batcher = self._batchers.pop(conversation_id, None)
        if batcher is not None:
            with contextlib.suppress(Exception):
                await batcher.close()

    async def _ensure_runtime(self, state: ConversationState) -> None:
        if state.binding is None:
            raise DomainError(ErrorCode.INVALID_STATE, "conversation has no binding")
        config = state.binding.configuration
        native = state.binding.native_session_id
        if native and not state.binding.requires_session_recreation:
            await self._runtime.resume(
                conversation_id=state.conversation.id,
                owner_id=state.conversation.owner_id,
                configuration=config,
                native_session_id=native,
                argv=(),
                **self._fence_kwargs(state.conversation.id),
            )
        else:
            await self._runtime.start(
                conversation_id=state.conversation.id,
                owner_id=state.conversation.owner_id,
                configuration=config,
                argv=(),
                **self._fence_kwargs(state.conversation.id),
            )

    def _ensure_pump(self, conversation_id: UUID) -> None:
        if self._draining:
            return
        existing = self._pumps.get(conversation_id)
        if existing is not None and not existing.done():
            return
        self._pumps[conversation_id] = asyncio.create_task(
            self._event_pump(conversation_id),
            name=f"event-pump-{conversation_id}",
        )

    async def _event_pump(self, conversation_id: UUID) -> None:
        managed = self._runtime.get_runtime(conversation_id)
        if managed is None:
            return

        async def flush(
            base_version: int,
            state: ConversationState,
            events: Sequence[ConversationEvent],
            commands: Sequence[Command],
        ) -> Sequence[ConversationEvent]:
            try:
                committed = await self._persistence.commit_turn_batch(
                    conversation_id,
                    base_version,
                    state,
                    events,
                    commands,
                    **self._fence_kwargs(conversation_id),
                )
            except DomainError as exc:
                if exc.code is ErrorCode.STALE_OWNER:
                    await self._on_stale_owner(conversation_id)
                    raise
                raise
            await checkpoint(self._fault_callback, FaultPoint.AFTER_EVENT_COMMIT)
            await self._safe_publish(committed, state=state)
            return committed

        batcher = DeltaBatcher(conversation_id=conversation_id, flush=flush)
        self._batchers[conversation_id] = batcher

        if isinstance(managed.adapter, _NativeDedupeAdapter):
            snapshot = await self._persistence.get_worker_snapshot(conversation_id)
            managed.adapter.import_seen(snapshot.seen_native_ids, snapshot.seen_stream_offsets)

        stream_ended = False
        stale_runtime = False
        try:
            async for event in managed.adapter.events(managed.session):
                if not await self._on_harness_event(
                    conversation_id,
                    event,
                    batcher,
                    managed_runtime=managed,
                ):
                    stale_runtime = True
                    break
            stream_ended = True
        except asyncio.CancelledError:
            raise
        except Exception:
            stream_ended = True
            logger.exception("event pump failed for %s", conversation_id)
        finally:
            with contextlib.suppress(Exception):
                if stale_runtime:
                    await batcher.discard()
                else:
                    await batcher.flush()
            current_task = asyncio.current_task()
            if self._pumps.get(conversation_id) is current_task:
                self._pumps.pop(conversation_id, None)
            if self._batchers.get(conversation_id) is batcher:
                self._batchers.pop(conversation_id, None)
            if stale_runtime and self._running and not self._draining:
                try:
                    snapshot = await self._persistence.get_worker_snapshot(conversation_id)
                    current = await self._runtime.ensure_binding_current(
                        conversation_id,
                        snapshot,
                    )
                    if current is None:
                        await self._ensure_runtime(snapshot)
                    self._ensure_pump(conversation_id)
                except Exception:
                    logger.exception("failed to replace stale runtime for %s", conversation_id)
            elif stream_ended and self._running and not self._draining:
                with contextlib.suppress(Exception):
                    await self._runtime.close(conversation_id, reason="event_stream_closed")

    async def _on_harness_event(
        self,
        conversation_id: UUID,
        event: HarnessEvent | HarnessInteractionRequest,
        batcher: DeltaBatcher,
        *,
        managed_runtime: ManagedRuntime | None = None,
    ) -> bool:
        async with self._lock_for(conversation_id):
            # Chain from pending batcher state when present so version stays coherent.
            state = batcher.state
            if state is None:
                state = await self._persistence.get_worker_snapshot(conversation_id)
                base_version = state.conversation.version
                authoritative = state
            else:
                authoritative = await self._persistence.get_worker_snapshot(conversation_id)
                base_version = authoritative.conversation.version

            # A stale runtime must never recreate history for a replaced binding.
            managed = managed_runtime or self._runtime.get_runtime(conversation_id)
            binding = authoritative.binding
            if managed is not None and (
                binding is None
                or binding.requires_session_recreation
                or managed.session.binding_id != binding.id
                or managed.session.native_session_id != binding.native_session_id
            ):
                logger.warning(
                    "discarding event from stale binding conversation=%s binding=%s",
                    conversation_id,
                    managed.session.binding_id,
                )
                await self._runtime.close_replaced_runtime(managed, reason="stale_binding")
                return False

            # Interaction requests force-flush through the broker (not the 50ms window).
            if isinstance(event, (HarnessInteractionRequest, InteractionRequestedPayload)):
                if self._broker is None:
                    raise DomainError(
                        ErrorCode.PERSISTENCE_REQUIRED,
                        "interaction broker is required",
                    )
                await batcher.flush()
                payload = event.payload if isinstance(event, HarnessInteractionRequest) else event
                pending = PendingInteraction(
                    id=payload.interaction_id,
                    conversation_id=conversation_id,
                    turn_id=payload.turn_id,
                    kind=payload.kind,
                    request=payload.request,
                    created_at=self._clock(),
                )
                await self._broker.accept_request(  # type: ignore[attr-defined]
                    conversation_id,
                    pending,
                    provider_correlation=(
                        event.provider_correlation
                        if isinstance(event, HarnessInteractionRequest)
                        else None
                    ),
                    **self._fence_kwargs(conversation_id),
                )
                return True

            now = self._clock()
            try:
                native_ids: tuple[str, ...] = ()
                stream_offsets: tuple[str, ...] = ()
                if managed is not None and isinstance(managed.adapter, _NativeDedupeAdapter):
                    seen_native, seen_offsets = managed.adapter.export_seen()
                    native_ids = tuple(seen_native - state.seen_native_ids)
                    stream_offsets = tuple(seen_offsets - state.seen_stream_offsets)
                result = dispatch_harness_event(
                    state,
                    event,
                    now=now,
                    native_ids=native_ids,
                    stream_offsets=stream_offsets,
                )
            except DomainError as exc:
                logger.warning("dispatch failed code=%s", exc.code.value)
                db_state = await self._persistence.get_worker_snapshot(conversation_id)
                if db_state.active_turn is not None:
                    ou = apply_outcome_unknown(
                        db_state,
                        now=now,
                        delivery_phase="event_dispatch",
                        message=public_message(exc.code),
                    )
                    committed = await self._persistence.commit_turn_batch(
                        conversation_id,
                        db_state.conversation.version,
                        ou.state,
                        ou.events,
                        tuple(
                            c
                            for c in ou.state.commands.values()
                            if c.status.value == "outcome_unknown"
                        ),
                        **self._fence_kwargs(conversation_id),
                    )
                    await self._safe_publish(committed, state=ou.state)
                await self._runtime.close(conversation_id, reason="protocol_error")
                return True

            await batcher.add(
                base_version=base_version,
                state=result.state,
                events=result.events,
                commands=result.commands,
                force=result.terminal,
            )
            if result.terminal:
                await self._wake_queued_submit(conversation_id)
            return True

    async def _wake_queued_submit(self, conversation_id: UUID) -> None:
        """Re-accept a deferred queued submit so claim can start the next turn."""
        state = await self._persistence.get_worker_snapshot(conversation_id)
        if state.active_turn is not None or state.queued_turn is None:
            return
        command_id = state.queued_turn.command_id
        if command_id is None:
            return
        command = state.commands.get(command_id)
        if (
            command is None
            or command.kind != CommandKind.SUBMIT_TURN
            or command.status != CommandStatus.CLAIMED
            or command.delivery_started_at is not None
        ):
            return
        released = command.model_copy(
            update={
                "status": CommandStatus.ACCEPTED,
                "worker_id": None,
                "lease_expires_at": None,
            }
        )
        commands = dict(state.commands)
        commands[released.id] = released
        next_state = state.model_copy(update={"commands": commands})
        await self._persistence.commit_turn_batch(
            conversation_id,
            state.conversation.version,
            next_state,
            (),
            (released,),
            **self._fence_kwargs(conversation_id),
        )

    async def _renew_lease(self, command: Command) -> None:
        if self._worker_id is None:
            return
        with contextlib.suppress(Exception):
            await self._persistence.renew_command_lease(
                command.id,
                self._worker_id,
                lease_duration=self._lease_seconds,
                fence=self._fences.get(command.conversation_id),
            )

    async def _safe_publish(
        self,
        events: Sequence[ConversationEvent],
        *,
        state: ConversationState | None = None,
    ) -> None:
        if not events:
            return
        get_observability().observe_committed_events(events, state=state)
        try:
            await self._publisher.publish(events)
            await checkpoint(self._fault_callback, FaultPoint.AFTER_PUBLICATION)
        except Exception:
            logger.exception("publisher failed after commit; events remain durable")
