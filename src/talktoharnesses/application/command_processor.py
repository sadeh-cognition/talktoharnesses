"""Provider-neutral durable command worker (explicit start/stop)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable
from uuid import UUID

from talktoharnesses.application.delta_batcher import DeltaBatcher
from talktoharnesses.application.event_dispatcher import (
    apply_outcome_unknown,
    dispatch_harness_event,
    mark_command_delivered,
    mark_command_delivery_started,
)
from talktoharnesses.application.persistence import Persistence
from talktoharnesses.application.publisher import CommittedEventPublisher
from talktoharnesses.domain.enums import CommandKind, CommandStatus, ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import (
    ConversationEvent,
    HarnessEvent,
    InteractionRequestedPayload,
)
from talktoharnesses.domain.models import (
    AnswerInteractionPayload,
    Command,
    InteractionAnswer,
    PendingInteraction,
    SteerPayload,
    SubmitTurnPayload,
)
from talktoharnesses.domain.transitions import ConversationState, apply_steer, start_turn
from talktoharnesses.providers.adapter import (
    HarnessAdapter,
    HarnessInteractionRequest,
    HarnessSession,
    SteerRequest,
    TurnRequest,
)
from talktoharnesses.runtime.manager import RuntimeManager

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._persistence = persistence
        self._publisher = publisher
        self._runtime = runtime_manager
        self._claim_limit = claim_limit
        self._lease_seconds = lease_seconds
        self._poll_interval = poll_interval
        self._clock = clock or (lambda: datetime.now(UTC))
        self._broker = interaction_broker

        self._worker_id: str | None = None
        self._running = False
        self._claim_task: asyncio.Task[None] | None = None
        self._command_tasks: set[asyncio.Task[None]] = set()
        self._conv_locks: dict[UUID, asyncio.Lock] = {}
        self._pumps: dict[UUID, asyncio.Task[None]] = {}
        self._batchers: dict[UUID, DeltaBatcher] = {}

    async def start(self, worker_id: str) -> None:
        if self._running:
            return
        self._worker_id = worker_id
        self._running = True
        self._claim_task = asyncio.create_task(self._claim_loop(), name=f"claim-{worker_id}")

    async def stop(self) -> None:
        self._running = False
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
                claimed = await self._persistence.claim_commands(
                    self._worker_id,
                    self._claim_limit,
                )
                for command in claimed:
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
            except Exception:
                logger.exception(
                    "command execution failed conversation=%s command=%s",
                    command.conversation_id,
                    command.id,
                )

    async def _execute_command(self, command: Command) -> None:
        state = await self._persistence.get_worker_snapshot(command.conversation_id)
        now = self._clock()

        # The aggregate command projection predates this worker's durable claim.
        # Carry the claimed row forward so later projection writes retain its
        # owner, lease, and attempt metadata.
        commands = dict(state.commands)
        commands[command.id] = command
        state = state.model_copy(update={"commands": commands})

        if self._runtime.get_runtime(command.conversation_id) is None:
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
            )
            await self._safe_publish(committed)
            state = await self._persistence.get_worker_snapshot(command.conversation_id)

        now = self._clock()
        state, started_cmd = mark_command_delivery_started(state, command.id, now=now)
        await self._persistence.update_command(started_cmd)

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
            if self._broker is not None:
                await self._broker.cancel_open_for_interrupt(command.conversation_id)  # type: ignore[attr-defined]
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
            await self._persistence.update_command(settled)
            return

        now = self._clock()
        state = await self._persistence.get_worker_snapshot(command.conversation_id)
        if command.id in state.commands:
            _, delivered_cmd = mark_command_delivered(state, command.id, now=now)
        else:
            delivered_cmd = started_cmd.model_copy(
                update={"status": CommandStatus.DELIVERED, "delivered_at": now}
            )
        await self._persistence.update_command(delivered_cmd)

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
            )
        except BaseException as exc:
            await asyncio.shield(self._mark_answer_outcome_unknown(command, exc))
            raise

    async def _mark_answer_outcome_unknown(
        self,
        command: Command,
        exc: BaseException,
    ) -> None:
        state = await self._persistence.get_worker_snapshot(command.conversation_id)
        current = state.commands.get(command.id, command)
        if current.status in {CommandStatus.SETTLED, CommandStatus.OUTCOME_UNKNOWN}:
            return
        failed = current.model_copy(
            update={
                "status": CommandStatus.OUTCOME_UNKNOWN,
                "recovery_result": str(exc) or type(exc).__name__,
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
        )
        await self._safe_publish(committed)

    async def _ensure_runtime(self, state: ConversationState) -> None:
        if state.binding is None:
            raise DomainError(ErrorCode.INVALID_STATE, "conversation has no binding")
        config = state.binding.configuration
        native = state.binding.native_session_id
        if native:
            await self._runtime.resume(
                conversation_id=state.conversation.id,
                owner_id=state.conversation.owner_id,
                configuration=config,
                native_session_id=native,
                argv=(),
            )
        else:
            await self._runtime.start(
                conversation_id=state.conversation.id,
                owner_id=state.conversation.owner_id,
                configuration=config,
                argv=(),
            )

    def _ensure_pump(self, conversation_id: UUID) -> None:
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
            committed = await self._persistence.commit_turn_batch(
                conversation_id,
                base_version,
                state,
                events,
                commands,
            )
            await self._safe_publish(committed)
            return committed

        batcher = DeltaBatcher(conversation_id=conversation_id, flush=flush)
        self._batchers[conversation_id] = batcher

        if isinstance(managed.adapter, _NativeDedupeAdapter):
            snapshot = await self._persistence.get_worker_snapshot(conversation_id)
            managed.adapter.import_seen(snapshot.seen_native_ids, snapshot.seen_stream_offsets)

        stream_ended = False
        try:
            async for event in managed.adapter.events(managed.session):
                await self._on_harness_event(conversation_id, event, batcher)
            stream_ended = True
        except asyncio.CancelledError:
            raise
        except Exception:
            stream_ended = True
            logger.exception("event pump failed for %s", conversation_id)
        finally:
            with contextlib.suppress(Exception):
                await batcher.flush()
            if stream_ended and self._running:
                with contextlib.suppress(Exception):
                    await self._runtime.close(conversation_id, reason="event_stream_closed")

    async def _on_harness_event(
        self,
        conversation_id: UUID,
        event: HarnessEvent | HarnessInteractionRequest,
        batcher: DeltaBatcher,
    ) -> None:
        async with self._lock_for(conversation_id):
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
                )
                return

            # Chain from pending batcher state when present so version stays coherent.
            state = batcher.state
            if state is None:
                state = await self._persistence.get_worker_snapshot(conversation_id)
            if batcher.state is None:
                base_version = state.conversation.version
            else:
                snap = await self._persistence.get_worker_snapshot(conversation_id)
                base_version = snap.conversation.version
            now = self._clock()
            try:
                native_ids: tuple[str, ...] = ()
                stream_offsets: tuple[str, ...] = ()
                managed = self._runtime.get_runtime(conversation_id)
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
                logger.warning("dispatch failed: %s", exc)
                db_state = await self._persistence.get_worker_snapshot(conversation_id)
                if db_state.active_turn is not None:
                    ou = apply_outcome_unknown(
                        db_state,
                        now=now,
                        delivery_phase="event_dispatch",
                        message=exc.message,
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
                    )
                    await self._safe_publish(committed)
                await self._runtime.close(conversation_id, reason="protocol_error")
                return

            await batcher.add(
                base_version=base_version,
                state=result.state,
                events=result.events,
                commands=result.commands,
                force=result.terminal,
            )
            if result.terminal:
                await self._wake_queued_submit(conversation_id)

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
        )

    async def _renew_lease(self, command: Command) -> None:
        if self._worker_id is None:
            return
        expires = self._clock() + timedelta(seconds=self._lease_seconds)
        with contextlib.suppress(Exception):
            await self._persistence.renew_command_lease(
                command.id,
                self._worker_id,
                expires,
            )

    async def _safe_publish(self, events: Sequence[ConversationEvent]) -> None:
        if not events:
            return
        try:
            await self._publisher.publish(events)
        except Exception:
            logger.exception("publisher failed after commit; events remain durable")
