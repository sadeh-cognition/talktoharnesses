"""Asynchronous Python facade over persistence, adapters, and durable commands."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from talktoharnesses.application.command_processor import CommandProcessor
from talktoharnesses.application.faults import FaultCallback
from talktoharnesses.application.interaction_broker import InteractionBroker
from talktoharnesses.application.observability import get_observability
from talktoharnesses.application.persistence import Persistence
from talktoharnesses.application.publisher import CommittedEventPublisher
from talktoharnesses.application.readiness import ReadinessProbeMonitor
from talktoharnesses.application.worker_coordinator import WorkerCoordinator
from talktoharnesses.domain.approval_matching import normalize_approval_rule
from talktoharnesses.domain.enums import (
    ActivityStatus,
    CommandKind,
    CommandStatus,
    ErrorCode,
    InteractionStatus,
)
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import ConversationEvent
from talktoharnesses.domain.models import (
    ActivityProjection,
    ApprovalRule,
    Command,
    CommandProjection,
    ConversationHarnessBinding,
    ConversationRuleScope,
    ConversationShell,
    ConversationSnapshot,
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessInstance,
    HarnessInstanceRuleScope,
    HarnessModeInfo,
    HarnessModelInfo,
    HarnessProbeProjection,
    HarnessProjection,
    InteractionProjection,
    InterruptPayload,
    MessageProjection,
    Page,
    PlanProjection,
    SubmitTurnPayload,
    SubmitTurnResult,
    SwitchHarnessPayload,
    ToolProjection,
    Turn,
    TurnProjection,
    UserRuleScope,
)
from talktoharnesses.domain.transitions import (
    ConversationState,
    TransitionResult,
    apply_steer,
    archive_conversation,
    cancel_queued_prompt,
    edit_queued_prompt,
    new_conversation_state,
    pin_conversation,
    snooze_conversation,
    soft_delete_conversation,
    submit_turn,
    unarchive_conversation,
    unpin_conversation,
    unsnooze_conversation,
)
from talktoharnesses.providers.registry import AdapterRegistry
from talktoharnesses.runtime.manager import RuntimeManager

logger = logging.getLogger(__name__)


def _command_projection(command: Command) -> CommandProjection:
    return CommandProjection(
        id=command.id,
        kind=command.kind,
        status=command.status,
        target_turn_id=command.target_turn_id,
        idempotency_key=command.idempotency_key,
        created_at=command.created_at,
    )


def _turn_projection(turn: Turn) -> TurnProjection:
    return TurnProjection(
        id=turn.id,
        conversation_id=turn.conversation_id,
        status=turn.status,
        user_message_id=turn.user_message_id,
        command_id=turn.command_id,
        created_at=turn.created_at,
        started_at=turn.started_at,
        completed_at=turn.completed_at,
        terminal_reason=turn.terminal_reason,
    )


class TalkToHarnessesService:
    """Single async facade for owner-scoped harness and conversation operations.

    Constructs and owns :class:`CommandProcessor`. Call :meth:`start` /
    :meth:`stop` explicitly; never from Django ``AppConfig.ready()``.
    """

    def __init__(
        self,
        persistence: Persistence,
        registry: AdapterRegistry,
        publisher: CommittedEventPublisher,
        clock: Callable[[], datetime],
        runtime_manager: RuntimeManager,
        *,
        fault_callback: FaultCallback = None,
    ) -> None:
        self._persistence = persistence
        self._registry = registry
        self._publisher = publisher
        self._clock = clock
        self._runtime = runtime_manager
        # Propagate into a pre-built runtime (production ASGI constructs it first).
        runtime_manager._fault_callback = fault_callback  # pyright: ignore[reportPrivateUsage]
        self._broker = InteractionBroker(persistence, publisher, clock=clock)
        self._processor = CommandProcessor(
            persistence,
            publisher,
            runtime_manager,
            clock=clock,
            interaction_broker=self._broker,
            lease_seconds=runtime_manager._policy.lease_duration,  # pyright: ignore[reportPrivateUsage]
            fault_callback=fault_callback,
        )
        self._coordinator = WorkerCoordinator(
            persistence,
            runtime_manager,
            publisher,
            self._processor,
            clock,
            runtime_manager._policy,  # pyright: ignore[reportPrivateUsage]
            fault_callback=fault_callback,
        )
        self._readiness = ReadinessProbeMonitor(persistence, registry, clock)
        self._started = False
        self._worker_id: str | None = None

    @property
    def processor(self) -> CommandProcessor:
        """Test/introspection access to the owned command processor."""
        return self._processor

    @property
    def coordinator(self) -> WorkerCoordinator:
        """Test/introspection access to the owned worker coordinator."""
        return self._coordinator

    @property
    def publisher(self) -> CommittedEventPublisher:
        """Committed-event publisher/broker used for SSE live delivery."""
        return self._publisher

    @property
    def started(self) -> bool:
        return self._started

    def readiness_snapshot(self) -> dict[str, bool]:
        """Worker readiness bits for the /ready route (WP5)."""
        snap = dict(self._coordinator.readiness_snapshot())
        snap["probe_fresh"] = self._readiness.is_fresh(self._clock())
        return snap

    async def is_ready(self) -> bool:
        """True when the worker is healthy and a harness probe is fresh."""
        if not self._started:
            return False
        snap = self._coordinator.readiness_snapshot()
        if not snap["ready_for_work"] or snap["draining"]:
            return False
        return await self._readiness.has_fresh_probe(self._clock())

    async def start(self, worker_id: str) -> None:
        """Start the durable command worker (idempotent)."""
        if self._started:
            return
        try:
            start = getattr(self._publisher, "start", None)
            if callable(start):
                await cast(Awaitable[None], start())
            await self._coordinator.acquire_and_heartbeat(worker_id)
            await self._broker.reconcile_on_startup()
            await self._coordinator.run_initial_recovery()
            await self._readiness.start()
            self._processor.set_claims_enabled(True)
            await self._processor.start(worker_id)
            self._worker_id = worker_id
            self._started = True
        except BaseException:
            deadline = time.monotonic() + self._runtime._policy.shutdown_budget  # pyright: ignore[reportPrivateUsage]
            await self._shutdown_components(deadline)
            raise

    async def stop(self) -> None:
        """Stop claims, then processor/runtime, then broker resources (idempotent)."""
        deadline = time.monotonic() + self._runtime._policy.shutdown_budget  # pyright: ignore[reportPrivateUsage]
        await self._shutdown_components(deadline)
        self._started = False
        self._worker_id = None

    async def _shutdown_components(self, deadline: float) -> None:
        await self._run_shutdown_step(
            self._coordinator.begin_shutdown(deadline), deadline, "coordinator_begin"
        )
        await self._run_shutdown_step(self._readiness.shutdown(deadline), deadline, "readiness")
        await self._run_shutdown_step(self._processor.stop(), deadline, "processor")
        await self._run_shutdown_step(
            self._coordinator.begin_shutdown(deadline),
            deadline,
            "coordinator_finalize_commands",
        )
        await self._run_shutdown_step(
            self._runtime.shutdown(deadline=deadline), deadline, "runtime"
        )
        stop = getattr(self._publisher, "stop", None)
        if callable(stop):
            await self._run_shutdown_step(cast(Awaitable[None], stop()), deadline, "publisher")
        await self._run_shutdown_step(
            self._coordinator.finish_shutdown(), deadline, "coordinator_finish"
        )

    @staticmethod
    async def _run_shutdown_step(
        awaitable: Awaitable[None],
        deadline: float,
        component: str,
    ) -> None:
        remaining = max(0.0, deadline - time.monotonic())
        task = asyncio.ensure_future(awaitable)
        try:
            await asyncio.wait_for(task, timeout=remaining)
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            logger.warning("shutdown_component_incomplete component=%s", component)
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            raise
        except Exception:
            logger.warning("shutdown_component_failed component=%s", component)

    # ------------------------------------------------------------------
    # Harnesses
    # ------------------------------------------------------------------

    async def create_harness(
        self,
        owner_id: str,
        *,
        name: str,
        configuration: HarnessConfiguration,
        harness_id: UUID | None = None,
    ) -> HarnessProjection:
        now = self._clock()
        instance = HarnessInstance(
            id=harness_id or uuid4(),
            owner_id=owner_id,
            name=name,
            kind=configuration.kind,
            configuration=configuration,
            created_at=now,
        )
        return await self._persistence.create_harness(instance)

    async def list_harnesses(
        self,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[HarnessProjection]:
        return await self._persistence.list_harnesses(owner_id, cursor=cursor, limit=limit)

    async def get_harness(self, owner_id: str, harness_id: UUID) -> HarnessProjection:
        return await self._persistence.get_harness(harness_id, owner_id)

    async def probe_harness(self, owner_id: str, harness_id: UUID) -> HarnessProbeProjection:
        harness = await self._persistence.get_harness(harness_id, owner_id)
        adapter = self._registry.create(harness.kind)
        try:
            capabilities = await adapter.probe(harness.configuration)
        except DomainError:
            raise
        except Exception as exc:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "harness probe failed",
                details={"harness_id": str(harness_id)},
            ) from exc
        probed_at = self._clock()
        projection = await self._persistence.save_harness_probe(
            harness_id,
            owner_id,
            capabilities,
            probed_at=probed_at,
        )
        self._readiness.notify_success(probed_at, harness_id)
        return projection

    async def get_harness_capabilities(
        self, owner_id: str, harness_id: UUID
    ) -> HarnessProbeProjection:
        return await self._persistence.get_harness_probe(harness_id, owner_id)

    async def get_harness_models(
        self, owner_id: str, harness_id: UUID
    ) -> tuple[HarnessModelInfo, ...]:
        probe = await self._persistence.get_harness_probe(harness_id, owner_id)
        return probe.capabilities.models

    async def get_harness_modes(
        self, owner_id: str, harness_id: UUID
    ) -> tuple[HarnessModeInfo, ...]:
        probe = await self._persistence.get_harness_probe(harness_id, owner_id)
        return probe.capabilities.modes

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------

    async def create_conversation(
        self,
        owner_id: str,
        harness_id: UUID,
        *,
        conversation_id: UUID | None = None,
        title: str | None = None,
    ) -> ConversationSnapshot:
        harness = await self._persistence.get_harness(harness_id, owner_id)
        now = self._clock()
        cid = conversation_id or uuid4()
        binding = ConversationHarnessBinding(
            conversation_id=cid,
            kind=harness.kind,
            configuration=harness.configuration,
            harness_instance_id=harness.id,
            created_at=now,
        )
        capabilities: HarnessCapabilities | None = None
        try:
            probe = await self._persistence.get_harness_probe(harness_id, owner_id)
            capabilities = probe.capabilities
        except DomainError:
            capabilities = None
        state = new_conversation_state(
            owner_id=owner_id,
            now=now,
            binding=binding,
            conversation_id=cid,
            capabilities=capabilities,
        )
        if title:
            state = state.model_copy(
                update={
                    "conversation": state.conversation.model_copy(
                        update={
                            "title_manual": title,
                            "current_binding_id": binding.id,
                        }
                    )
                }
            )
        else:
            state = state.model_copy(
                update={
                    "conversation": state.conversation.model_copy(
                        update={"current_binding_id": binding.id}
                    )
                }
            )
        await self._persistence.save_snapshot(state)
        return await self._persistence.get_conversation_snapshot(cid, owner_id)

    async def list_conversations(
        self,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
        include_archived: bool = True,
    ) -> Page[ConversationShell]:
        return await self._persistence.list_conversations(
            owner_id,
            cursor=cursor,
            limit=limit,
            include_archived=include_archived,
        )

    async def search_conversations(
        self,
        owner_id: str,
        query: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[ConversationShell]:
        return await self._persistence.search_conversations(
            owner_id, query, cursor=cursor, limit=limit
        )

    async def get_conversation(self, owner_id: str, conversation_id: UUID) -> ConversationSnapshot:
        return await self._persistence.get_conversation_snapshot(conversation_id, owner_id)

    async def archive_conversation(
        self, owner_id: str, conversation_id: UUID
    ) -> ConversationSnapshot:
        return await self._mutate(owner_id, conversation_id, archive_conversation)

    async def unarchive_conversation(
        self, owner_id: str, conversation_id: UUID
    ) -> ConversationSnapshot:
        return await self._mutate(owner_id, conversation_id, unarchive_conversation)

    async def pin_conversation(self, owner_id: str, conversation_id: UUID) -> ConversationSnapshot:
        return await self._mutate(owner_id, conversation_id, pin_conversation)

    async def unpin_conversation(
        self, owner_id: str, conversation_id: UUID
    ) -> ConversationSnapshot:
        return await self._mutate(owner_id, conversation_id, unpin_conversation)

    async def snooze_conversation(
        self,
        owner_id: str,
        conversation_id: UUID,
        *,
        until: datetime,
    ) -> ConversationSnapshot:
        def _fn(state: ConversationState, *, now: datetime) -> TransitionResult:
            return snooze_conversation(state, now=now, until=until)

        return await self._mutate(owner_id, conversation_id, _fn)

    async def unsnooze_conversation(
        self, owner_id: str, conversation_id: UUID
    ) -> ConversationSnapshot:
        return await self._mutate(owner_id, conversation_id, unsnooze_conversation)

    async def soft_delete_conversation(self, owner_id: str, conversation_id: UUID) -> None:
        state = await self._persistence.get_snapshot(conversation_id, owner_id)
        result = soft_delete_conversation(state, now=self._clock())
        events = await self._persistence.commit_facade_mutation(
            conversation_id,
            owner_id,
            state.conversation.version,
            result.state,
            result.events,
        )
        await self._publish(events)

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    async def page_turns(
        self,
        owner_id: str,
        conversation_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[TurnProjection]:
        return await self._persistence.page_turns(
            conversation_id, owner_id, cursor=cursor, limit=limit
        )

    async def page_messages(
        self,
        owner_id: str,
        conversation_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[MessageProjection]:
        return await self._persistence.page_messages(
            conversation_id, owner_id, cursor=cursor, limit=limit
        )

    async def page_tools(
        self,
        owner_id: str,
        conversation_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[ToolProjection]:
        return await self._persistence.page_tools(
            conversation_id, owner_id, cursor=cursor, limit=limit
        )

    async def page_plans(
        self,
        owner_id: str,
        conversation_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[PlanProjection]:
        return await self._persistence.page_plans(
            conversation_id, owner_id, cursor=cursor, limit=limit
        )

    async def page_activity(
        self,
        owner_id: str,
        conversation_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[ActivityProjection]:
        return await self._persistence.page_activity(
            conversation_id, owner_id, cursor=cursor, limit=limit
        )

    # ------------------------------------------------------------------
    # Turn control
    # ------------------------------------------------------------------

    async def submit_turn(
        self,
        owner_id: str,
        conversation_id: UUID,
        *,
        prompt: str,
        idempotency_key: str,
        model: str | None = None,
    ) -> SubmitTurnResult:
        if not idempotency_key or not idempotency_key.strip():
            raise DomainError(ErrorCode.INVALID_STATE, "Idempotency-Key is required")
        state = await self._persistence.get_snapshot(conversation_id, owner_id)
        result = submit_turn(
            state,
            prompt=prompt,
            idempotency_key=idempotency_key,
            now=self._clock(),
            model=model,
        )
        if result.command is None:
            raise DomainError(ErrorCode.INVALID_STATE, "submit_turn produced no command")

        # Idempotent replay: same key + different payload is a conflict.
        if not result.events:
            existing = result.command
            if isinstance(existing.payload, SubmitTurnPayload) and (
                existing.payload.prompt != prompt or existing.payload.model != model
            ):
                raise DomainError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "idempotency key reused with a different payload",
                    details={"idempotency_key": idempotency_key},
                )
            turn = self._target_turn(result.state, existing)
            return SubmitTurnResult(
                command=_command_projection(existing),
                turn=_turn_projection(turn),
            )

        events = await self._persistence.commit_facade_mutation(
            conversation_id,
            owner_id,
            state.conversation.version,
            result.state,
            result.events,
            commands=(result.command,),
        )
        await self._publish(events)
        turn = self._target_turn(result.state, result.command)
        return SubmitTurnResult(
            command=_command_projection(result.command),
            turn=_turn_projection(turn),
        )

    async def edit_queued_prompt(
        self,
        owner_id: str,
        conversation_id: UUID,
        *,
        prompt: str,
    ) -> ConversationSnapshot:
        state = await self._persistence.get_snapshot(conversation_id, owner_id)
        result = edit_queued_prompt(state, prompt=prompt, now=self._clock())
        events = await self._persistence.commit_facade_mutation(
            conversation_id,
            owner_id,
            state.conversation.version,
            result.state,
            result.events,
        )
        await self._publish(events)
        return await self._persistence.get_conversation_snapshot(conversation_id, owner_id)

    async def cancel_queued_prompt(
        self, owner_id: str, conversation_id: UUID
    ) -> CommandProjection | None:
        state = await self._persistence.get_snapshot(conversation_id, owner_id)
        queued_cmd_id = state.queued_turn.command_id if state.queued_turn else None
        result = cancel_queued_prompt(state, now=self._clock())
        commands: tuple[Command, ...] = ()
        projection: CommandProjection | None = None
        if queued_cmd_id is not None and queued_cmd_id in result.state.commands:
            settled = result.state.commands[queued_cmd_id]
            commands = (settled,)
            projection = _command_projection(settled)
        events = await self._persistence.commit_facade_mutation(
            conversation_id,
            owner_id,
            state.conversation.version,
            result.state,
            result.events,
            commands=commands,
        )
        await self._publish(events)
        return projection

    async def steer(
        self,
        owner_id: str,
        conversation_id: UUID,
        *,
        prompt: str,
        idempotency_key: str,
    ) -> CommandProjection:
        if not idempotency_key or not idempotency_key.strip():
            raise DomainError(ErrorCode.INVALID_STATE, "Idempotency-Key is required")
        state = await self._persistence.get_snapshot(conversation_id, owner_id)
        result = apply_steer(
            state,
            prompt=prompt,
            idempotency_key=idempotency_key,
            now=self._clock(),
        )
        if result.command is None:
            raise DomainError(ErrorCode.INVALID_STATE, "steer produced no command")
        if not result.events:
            return _command_projection(result.command)
        events = await self._persistence.commit_facade_mutation(
            conversation_id,
            owner_id,
            state.conversation.version,
            result.state,
            result.events,
            commands=(result.command,),
        )
        await self._publish(events)
        return _command_projection(result.command)

    async def interrupt(
        self,
        owner_id: str,
        conversation_id: UUID,
        *,
        idempotency_key: str | None = None,
    ) -> CommandProjection:
        """Persist an interrupt command for the worker; does not complete the turn here."""
        state = await self._persistence.get_snapshot(conversation_id, owner_id)
        if state.active_turn is None:
            raise DomainError(ErrorCode.NO_ACTIVE_TURN, "no active turn to interrupt")
        now = self._clock()
        key = idempotency_key or f"interrupt:{state.active_turn.id}:{uuid4()}"
        for existing in state.commands.values():
            if existing.idempotency_key == key:
                return _command_projection(existing)
        command = Command(
            conversation_id=conversation_id,
            kind=CommandKind.INTERRUPT,
            status=CommandStatus.ACCEPTED,
            idempotency_key=key,
            target_turn_id=state.active_turn.id,
            payload=InterruptPayload(),
            created_at=now,
        )
        commands = dict(state.commands)
        commands[command.id] = command
        new_state = state.model_copy(
            update={
                "commands": commands,
                "conversation": state.conversation.model_copy(
                    update={"version": state.conversation.version + 1, "updated_at": now}
                ),
            }
        )
        events = await self._persistence.commit_facade_mutation(
            conversation_id,
            owner_id,
            state.conversation.version,
            new_state,
            (),
            commands=(command,),
        )
        await self._publish(events)
        return _command_projection(command)

    # ------------------------------------------------------------------
    # Harness switching
    # ------------------------------------------------------------------

    async def switch_harness(
        self,
        owner_id: str,
        conversation_id: UUID,
        *,
        harness_id: UUID,
        idempotency_key: str,
    ) -> CommandProjection:
        """Accept a durable switch to another owned harness on an idle conversation."""
        if not idempotency_key or not idempotency_key.strip():
            raise DomainError(ErrorCode.INVALID_STATE, "Idempotency-Key is required")
        state = await self._persistence.get_snapshot(conversation_id, owner_id)

        for existing in state.commands.values():
            if existing.idempotency_key != idempotency_key:
                continue
            if (
                not isinstance(existing.payload, SwitchHarnessPayload)
                or existing.payload.harness_instance_id != harness_id
            ):
                raise DomainError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "idempotency key reused with a different payload",
                    details={"idempotency_key": idempotency_key},
                )
            return _command_projection(existing)

        harness = await self._persistence.get_harness(harness_id, owner_id)
        if (
            state.active_turn is not None
            or state.queued_turn is not None
            or any(a.status is ActivityStatus.RUNNING for a in state.activities.values())
        ):
            raise DomainError(
                ErrorCode.CONVERSATION_BUSY,
                "conversation must be idle to switch harness",
                details={"conversation_id": str(conversation_id)},
            )
        await self._validate_switch_target(owner_id, harness)

        now = self._clock()
        command = Command(
            conversation_id=conversation_id,
            kind=CommandKind.SWITCH_HARNESS,
            status=CommandStatus.ACCEPTED,
            idempotency_key=idempotency_key,
            payload=SwitchHarnessPayload(
                configuration=harness.configuration,
                harness_instance_id=harness.id,
            ),
            created_at=now,
        )
        commands = dict(state.commands)
        commands[command.id] = command
        new_state = state.model_copy(
            update={
                "commands": commands,
                "conversation": state.conversation.model_copy(
                    update={"version": state.conversation.version + 1, "updated_at": now}
                ),
            }
        )
        events = await self._persistence.commit_facade_mutation(
            conversation_id,
            owner_id,
            state.conversation.version,
            new_state,
            (),
            commands=(command,),
        )
        await self._publish(events)
        return _command_projection(command)

    async def _validate_switch_target(self, owner_id: str, harness: HarnessProjection) -> None:
        """Require a successful probe and a supported finite model/mode."""
        try:
            probe = await self._persistence.get_harness_probe(harness.id, owner_id)
        except DomainError as exc:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "target harness has no successful probe",
                details={"harness_id": str(harness.id)},
            ) from exc
        configuration = harness.configuration
        capabilities = probe.capabilities
        if (
            configuration.model
            and capabilities.models
            and all(m.id != configuration.model for m in capabilities.models)
        ):
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "target harness does not support the requested model",
                details={"model": configuration.model},
            )
        if (
            configuration.mode
            and capabilities.modes
            and all(m.id != configuration.mode for m in capabilities.modes)
        ):
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "target harness does not support the requested mode",
                details={"mode": configuration.mode},
            )

    # ------------------------------------------------------------------
    # Interactions
    # ------------------------------------------------------------------

    async def list_pending_interactions(
        self,
        owner_id: str,
        conversation_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[InteractionProjection]:
        return await self._persistence.page_pending_interactions(
            conversation_id, owner_id, cursor=cursor, limit=limit
        )

    async def update_interaction_draft(
        self,
        owner_id: str,
        conversation_id: UUID,
        interaction_id: UUID,
        *,
        draft: dict[str, Any],
    ) -> InteractionProjection:
        return await self._broker.update_draft(
            owner_id, conversation_id, interaction_id, draft=draft
        )

    async def resolve_interaction(
        self,
        owner_id: str,
        conversation_id: UUID,
        interaction_id: UUID,
        *,
        decision: Any = None,
        answers: dict[str, Any] | None = None,
        create_rule: Any = None,
        idempotency_key: str | None = None,
    ) -> CommandProjection:
        return await self._broker.resolve_manual(
            owner_id,
            conversation_id,
            interaction_id,
            decision=decision,
            answers=answers,
            create_rule=create_rule,
            idempotency_key=idempotency_key,
        )

    # ------------------------------------------------------------------
    # Approval rules and audits
    # ------------------------------------------------------------------

    async def create_approval_rule(self, owner_id: str, rule: Any) -> Any:
        normalized = await self._normalize_owned_approval_rule(owner_id, rule)
        return await self._persistence.create_approval_rule(normalized)

    async def list_approval_rules(
        self, owner_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> Any:
        return await self._persistence.page_approval_rules(owner_id, cursor=cursor, limit=limit)

    async def get_approval_rule(self, owner_id: str, rule_id: UUID) -> Any:
        return await self._persistence.get_approval_rule(rule_id, owner_id)

    async def replace_approval_rule(self, owner_id: str, rule: Any) -> Any:
        normalized = await self._normalize_owned_approval_rule(owner_id, rule)
        return await self._persistence.replace_approval_rule(normalized)

    async def _normalize_owned_approval_rule(
        self,
        owner_id: str,
        rule: Any,
    ) -> ApprovalRule:
        if not isinstance(rule, ApprovalRule):
            raise DomainError(ErrorCode.INVALID_STATE, "invalid approval rule")
        if rule.principal_id != owner_id:
            raise DomainError(ErrorCode.INVALID_STATE, "rule principal must match caller")
        if isinstance(rule.scope, ConversationRuleScope):
            await self._persistence.get_snapshot(rule.scope.conversation_id, owner_id)
        elif isinstance(rule.scope, HarnessInstanceRuleScope):
            await self._persistence.get_harness(rule.scope.harness_instance_id, owner_id)
        elif isinstance(rule.scope, UserRuleScope) and rule.scope.user_id != owner_id:
            raise DomainError(ErrorCode.INVALID_STATE, "rule user scope must match caller")
        try:
            return normalize_approval_rule(rule)
        except ValueError as exc:
            raise DomainError(ErrorCode.INVALID_STATE, str(exc)) from exc

    async def delete_approval_rule(self, owner_id: str, rule_id: UUID) -> None:
        await self._persistence.delete_approval_rule(rule_id, owner_id)

    async def list_interaction_audits(
        self, owner_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> Any:
        return await self._persistence.page_interaction_audits(owner_id, cursor=cursor, limit=limit)

    async def get_interaction_audit(self, owner_id: str, audit_id: UUID) -> Any:
        return await self._persistence.get_interaction_audit(audit_id, owner_id)

    # ------------------------------------------------------------------
    # Event sync
    # ------------------------------------------------------------------

    async def get_snapshot(self, owner_id: str, conversation_id: UUID) -> ConversationSnapshot:
        return await self._persistence.get_conversation_snapshot(conversation_id, owner_id)

    async def get_high_water_sequence(self, owner_id: str, conversation_id: UUID) -> int:
        return await self._persistence.get_high_water_sequence(conversation_id, owner_id)

    async def get_stream_high_water_sequence(
        self,
        owner_id: str,
        conversation_id: UUID,
    ) -> int:
        """Owner-scoped high water for an already-authorized live stream."""
        return await self._persistence.get_high_water_sequence(
            conversation_id,
            owner_id,
            include_deleted=True,
        )

    async def get_stream_snapshot(
        self,
        owner_id: str,
        conversation_id: UUID,
    ) -> ConversationSnapshot:
        """Owner-scoped snapshot for an already-authorized live stream."""
        return await self._persistence.get_conversation_snapshot(
            conversation_id,
            owner_id,
            include_deleted=True,
        )

    async def replay_events(
        self,
        owner_id: str,
        conversation_id: UUID,
        *,
        after_sequence: int,
        event_count_limit: int = 5000,
        byte_limit: int = 5 * 1024 * 1024,
    ) -> Sequence[ConversationEvent]:
        # Owner-scope first (cross-owner / deleted look like missing).
        await self._persistence.get_conversation_snapshot(conversation_id, owner_id)
        return await self._persistence.replay(
            conversation_id,
            after_sequence,
            event_count_limit,
            byte_limit,
        )

    async def replay_stream_events(
        self,
        owner_id: str,
        conversation_id: UUID,
        *,
        after_sequence: int,
        event_count_limit: int = 5000,
        byte_limit: int = 5 * 1024 * 1024,
    ) -> Sequence[ConversationEvent]:
        """Replay for a stream authorized before a possible soft delete."""
        await self.get_stream_high_water_sequence(owner_id, conversation_id)
        return await self._persistence.replay(
            conversation_id,
            after_sequence,
            event_count_limit,
            byte_limit,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _mutate(
        self,
        owner_id: str,
        conversation_id: UUID,
        transition: Callable[..., TransitionResult],
    ) -> ConversationSnapshot:
        state = await self._persistence.get_snapshot(conversation_id, owner_id)
        result = transition(state, now=self._clock())
        events = await self._persistence.commit_facade_mutation(
            conversation_id,
            owner_id,
            state.conversation.version,
            result.state,
            result.events,
        )
        await self._publish(events)
        # Soft-deleted conversations are not readable after commit.
        if result.state.conversation.deleted_at is not None:
            raise DomainError(ErrorCode.NOT_FOUND, "conversation not found")
        return await self._persistence.get_conversation_snapshot(conversation_id, owner_id)

    async def _publish(self, events: Sequence[ConversationEvent]) -> None:
        if events:
            get_observability().observe_committed_events(events)
            await self._publisher.publish(events)

    @staticmethod
    def _target_turn(state: ConversationState, command: Command) -> Turn:
        if command.target_turn_id is not None:
            if state.queued_turn is not None and state.queued_turn.id == command.target_turn_id:
                return state.queued_turn
            if state.active_turn is not None and state.active_turn.id == command.target_turn_id:
                return state.active_turn
        if state.queued_turn is not None:
            return state.queued_turn
        if state.active_turn is not None:
            return state.active_turn
        raise DomainError(ErrorCode.INVALID_STATE, "command has no target turn")


# Silence unused import used only for type documentation of pending status.
_ = InteractionStatus


def utc_clock() -> datetime:
    """Default UTC clock for production wiring."""
    return datetime.now(UTC)
