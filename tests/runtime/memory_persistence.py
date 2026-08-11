"""In-memory Persistence for runtime lifecycle and facade contract tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from talktoharnesses.application.cursors import (
    clamp_page_limit,
    decode_cursor,
    decode_search_cursor,
    encode_cursor,
    encode_search_cursor,
)
from talktoharnesses.application.handoff import (
    HandoffDocument,
    HandoffMessage,
    HandoffTool,
    handoff_sort_key,
)
from talktoharnesses.application.persistence import (
    ClaimedCommand,
    ConversationOwnership,
    LostLease,
    PruneResult,
    RecoveryAttempt,
    SwitchPreparation,
)
from talktoharnesses.application.search_documents import (
    SearchDocumentFields,
    build_search_document_from_parts,
)
from talktoharnesses.application.search_query import (
    build_snippet,
    count_token_occurrences,
    document_matches_exclusions,
    parse_search_query,
    rank_document,
)
from talktoharnesses.domain.approval_matching import (
    InteractionMatchContext,
    select_matching_rule,
)
from talktoharnesses.domain.enums import (
    ApprovalDecision,
    ApprovalRuleDecision,
    CommandStatus,
    ConversationStatus,
    ErrorCode,
    InteractionKind,
    InteractionStatus,
    MessageRole,
    ObservedDeliveryPhase,
    ProcessStatus,
    RecoveryAction,
    RecoveryReasonCode,
    RecoveryResultCode,
    RecoveryTrigger,
    ToolOutcome,
    TurnStatus,
)
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import (
    AssistantMessageCompletedPayload,
    AssistantMessageStartedPayload,
    ConversationEvent,
    ToolCompletedPayload,
    ToolRequestedPayload,
    TurnCancelledPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
    TurnInterruptedPayload,
    TurnOutcomeUnknownPayload,
    TurnQueuedPayload,
    TurnStartedPayload,
    event_turn_id,
)
from talktoharnesses.domain.models import (
    ActivityProjection,
    ApprovalRequestPayload,
    ApprovalRule,
    ApprovalRuleProjection,
    BackgroundActivity,
    CanonicalToolResult,
    Command,
    CommandProjection,
    ConversationDetail,
    ConversationSearchHit,
    ConversationShell,
    ConversationSnapshot,
    HarnessCapabilities,
    HarnessInstance,
    HarnessProbeProjection,
    HarnessProjection,
    InteractionAnswer,
    InteractionAuditProjection,
    InteractionProjection,
    InteractionResolutionResult,
    LaunchSnapshot,
    Message,
    MessageProjection,
    Page,
    PendingInteraction,
    Plan,
    PlanProjection,
    ProcessRecord,
    RetentionPolicyProjection,
    RetentionPreviewProjection,
    ToolProjection,
    Turn,
    TurnProjection,
)
from talktoharnesses.domain.transitions import (
    ConversationState,
    interrupt_turn,
    mark_requires_recreation,
    rotate_session,
    submit_interaction_answer,
)

_SQLITE_SUPERVISOR_SLOT = "sqlite-supervisor"
_ACTIVE_RECOVERY_STATUSES = {
    ConversationStatus.RUNNING,
    ConversationStatus.WAITING,
    ConversationStatus.BACKGROUND_ACTIVE,
}
_RENEWABLE_COMMAND_STATUSES = {
    CommandStatus.CLAIMED,
    CommandStatus.DELIVERY_STARTED,
    CommandStatus.DELIVERED,
}


def _not_found(resource: str = "conversation") -> DomainError:
    return DomainError(ErrorCode.NOT_FOUND, f"{resource} not found")


def _stale_owner(*, conversation_id: UUID | None = None) -> DomainError:
    details: dict[str, object] = {}
    if conversation_id is not None:
        details["conversation_id"] = str(conversation_id)
    return DomainError(ErrorCode.STALE_OWNER, "stale conversation owner", details=details)


class MemoryPersistence:
    """Minimal durable double implementing the Persistence protocol."""

    def __init__(self) -> None:
        self.states: dict[UUID, ConversationState] = {}
        self.processes: dict[UUID, ProcessRecord] = {}  # process_id -> record
        self.launch_history: dict[UUID, list[LaunchSnapshot]] = {}  # conversation
        self.events: dict[UUID, list[ConversationEvent]] = {}
        self.commands: dict[UUID, Command] = {}
        self.accepted_queue: list[UUID] = []
        self.harnesses: dict[UUID, HarnessInstance] = {}
        self.harness_probes: dict[UUID, tuple[HarnessCapabilities, datetime]] = {}
        # Projection stores keyed by conversation then entity id.
        self.turns: dict[UUID, dict[UUID, Turn]] = {}
        self.messages: dict[UUID, dict[UUID, Message]] = {}
        self.tools: dict[UUID, dict[UUID, CanonicalToolResult]] = {}
        self.plans: dict[UUID, dict[UUID, Plan]] = {}
        self.activities: dict[UUID, dict[UUID, BackgroundActivity]] = {}
        self.interactions: dict[UUID, dict[UUID, PendingInteraction]] = {}
        self.interaction_answers: dict[UUID, InteractionAnswer] = {}
        self.interaction_meta: dict[UUID, dict[str, object]] = {}
        self.approval_rules: dict[UUID, ApprovalRule] = {}
        self.interaction_audits: dict[UUID, InteractionAuditProjection] = {}
        self.search_docs: dict[UUID, SearchDocumentFields] = {}
        self.turn_order: dict[UUID, list[UUID]] = {}
        # Retained item order for handoff export (message/tool id -> order_index).
        self.item_order_index: dict[UUID, dict[UUID, int]] = {}
        # Phase 9 ownership / recovery doubles.
        self.ownership: dict[UUID, tuple[str, int, datetime]] = {}
        self.worker_leases: dict[str, dict[str, object]] = {}
        self.recovery_attempts: dict[UUID, RecoveryAttempt] = {}
        self.retention_policies: dict[str, RetentionPolicyProjection] = {}
        self._sqlite_mode: bool = True

    def seed(self, state: ConversationState) -> None:
        self.states[state.conversation.id] = state
        self.events.setdefault(state.conversation.id, [])
        self.launch_history.setdefault(state.conversation.id, [])
        self.turns.setdefault(state.conversation.id, {})
        self.messages.setdefault(state.conversation.id, {})
        self.tools.setdefault(state.conversation.id, {})
        self.plans.setdefault(state.conversation.id, {})
        self.activities.setdefault(state.conversation.id, {})
        self.interactions.setdefault(state.conversation.id, {})
        self.turn_order.setdefault(state.conversation.id, [])
        self._refresh_search(state)

    async def get_snapshot(self, conversation_id: UUID, owner_id: str) -> ConversationState:
        try:
            state = self.states[conversation_id]
        except KeyError as exc:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "conversation not found",
                details={"conversation_id": str(conversation_id)},
            ) from exc
        if state.conversation.owner_id != owner_id:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "owner mismatch",
                details={"conversation_id": str(conversation_id)},
            )
        if state.conversation.deleted_at is not None:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "conversation not found",
                details={"conversation_id": str(conversation_id)},
            )
        return state

    async def get_worker_snapshot(self, conversation_id: UUID) -> ConversationState:
        try:
            return self.states[conversation_id]
        except KeyError as exc:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "conversation not found",
                details={"conversation_id": str(conversation_id)},
            ) from exc

    async def save_snapshot(self, state: ConversationState) -> ConversationState:
        self.states[state.conversation.id] = state
        self.events.setdefault(state.conversation.id, [])
        self.launch_history.setdefault(state.conversation.id, [])
        self.turns.setdefault(state.conversation.id, {})
        self.messages.setdefault(state.conversation.id, {})
        self.tools.setdefault(state.conversation.id, {})
        self.plans.setdefault(state.conversation.id, {})
        self.activities.setdefault(state.conversation.id, {})
        self.interactions.setdefault(state.conversation.id, {})
        self.turn_order.setdefault(state.conversation.id, [])
        self._index_state_projections(state)
        self._refresh_search(state)
        return state

    async def accept_command(self, command: Command) -> Command:
        existing = self.commands.get(command.id)
        if existing is not None:
            return existing
        for stored in self.commands.values():
            if (
                stored.conversation_id == command.conversation_id
                and stored.idempotency_key == command.idempotency_key
            ):
                return stored
        self.commands[command.id] = command
        self.accepted_queue.append(command.id)
        return command

    async def claim_commands(
        self,
        worker_id: str,
        limit: int,
        *,
        lease_duration: float,
    ) -> Sequence[ClaimedCommand]:
        claimed: list[ClaimedCommand] = []
        still_pending: list[UUID] = []
        now = datetime.now(UTC)
        lease = now + timedelta(seconds=lease_duration)
        candidates = list(self.accepted_queue)
        candidates.extend(
            command.id
            for command in self.commands.values()
            if command.status == CommandStatus.CLAIMED
            and command.lease_expires_at is not None
            and command.lease_expires_at < now
            and command.id not in self.accepted_queue
        )
        for command_id in candidates:
            if len(claimed) >= limit:
                still_pending.append(command_id)
                continue
            command = self.commands.get(command_id)
            if command is None or (
                command.status != CommandStatus.ACCEPTED
                and not (
                    command.status == CommandStatus.CLAIMED
                    and command.lease_expires_at is not None
                    and command.lease_expires_at < now
                )
            ):
                continue
            fence = self._acquire_conversation_owner(
                command.conversation_id,
                worker_id,
                lease_duration=lease_duration,
                now=now,
            )
            if fence is None:
                still_pending.append(command_id)
                continue
            updated = command.model_copy(
                update={
                    "status": CommandStatus.CLAIMED,
                    "worker_id": worker_id,
                    "attempts": command.attempts + 1,
                    "lease_expires_at": lease,
                }
            )
            self.commands[command_id] = updated
            claimed.append(ClaimedCommand(command=updated, fence=fence))
        self.accepted_queue = still_pending
        return tuple(claimed)

    async def renew_command_lease(
        self,
        command_id: UUID,
        worker_id: str,
        *,
        lease_duration: float,
        fence: int | None = None,
    ) -> None:
        command = self.commands.get(command_id)
        if (
            command is None
            or command.worker_id != worker_id
            or command.status not in _RENEWABLE_COMMAND_STATUSES
        ):
            raise DomainError(ErrorCode.INVALID_STATE, "command lease not found for worker")
        if fence is not None:
            self._require_owner(command.conversation_id, worker_id, fence)
        self.commands[command_id] = command.model_copy(
            update={"lease_expires_at": datetime.now(UTC) + timedelta(seconds=lease_duration)}
        )

    async def update_command(
        self,
        command: Command,
        *,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> Command:
        if worker_id is not None or fence is not None:
            self._require_owner(command.conversation_id, worker_id, fence)
        self.commands[command.id] = command
        if command.status == CommandStatus.ACCEPTED and command.id not in self.accepted_queue:
            self.accepted_queue.append(command.id)
        return command

    async def commit_event_batch(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
        *,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> Sequence[ConversationEvent]:
        return await self.commit_turn_batch(
            conversation_id,
            expected_version,
            state,
            events,
            (),
            worker_id=worker_id,
            fence=fence,
        )

    async def commit_turn_batch(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
        commands: Sequence[Command] = (),
        *,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> Sequence[ConversationEvent]:
        committed = await self.commit_runtime_lifecycle(
            conversation_id,
            expected_version,
            state,
            None,
            None,
            events,
            worker_id=worker_id,
            fence=fence,
        )
        for command in commands:
            self.commands[command.id] = command
            if command.status == CommandStatus.ACCEPTED and command.id not in self.accepted_queue:
                self.accepted_queue.append(command.id)
        return committed

    async def commit_runtime_lifecycle(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        process: ProcessRecord | None,
        launch_history_entry: LaunchSnapshot | None,
        events: Sequence[ConversationEvent],
        *,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> Sequence[ConversationEvent]:
        if worker_id is not None or fence is not None:
            self._require_owner(conversation_id, worker_id, fence)
        current = self.states.get(conversation_id)
        if current is None:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "conversation not found",
                details={"conversation_id": str(conversation_id)},
            )
        if current.conversation.version != expected_version:
            raise DomainError(
                ErrorCode.OPTIMISTIC_CONFLICT,
                "optimistic concurrency conflict",
                details={
                    "expected": expected_version,
                    "actual": current.conversation.version,
                },
            )

        if process is not None:
            existing = self.processes.get(process.id)
            if existing is not None and existing.status == process.status:
                pass
            self.processes[process.id] = process
            if process.redacted_stderr_tail or existing is None:
                self.processes[process.id] = process

        if launch_history_entry is not None:
            history = self.launch_history.setdefault(conversation_id, [])
            history.append(launch_history_entry)

        stored_events = self.events.setdefault(conversation_id, [])
        if events:
            for event in events:
                stored_events.append(event)
            self.states[conversation_id] = state
        else:
            self.states[conversation_id] = (
                state.model_copy(
                    update={
                        "conversation": state.conversation.model_copy(
                            update={"version": current.conversation.version}
                        )
                    }
                )
                if state.conversation.version != current.conversation.version
                else state
            )
            if state.conversation.version != expected_version:
                self.states[conversation_id] = current.model_copy(
                    update={
                        "binding": state.binding,
                        "idle_reap_eligible": state.idle_reap_eligible,
                    }
                )
            else:
                self.states[conversation_id] = state

        self._index_state_projections(self.states[conversation_id])
        self._apply_events_to_projections(conversation_id, events)
        self._refresh_search(self.states[conversation_id])
        return tuple(events)

    async def replay(
        self,
        conversation_id: UUID,
        after_sequence: int,
        event_count_limit: int,
        byte_limit: int,
    ) -> Sequence[ConversationEvent]:
        items = [e for e in self.events.get(conversation_id, []) if e.sequence > after_sequence]
        return tuple(items[:event_count_limit])

    async def commit_interaction_request(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
        *,
        interaction_id: UUID,
        provider_correlation: dict[str, str] | None = None,
        request_event_sequence: int,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> Sequence[ConversationEvent]:
        committed = await self.commit_turn_batch(
            conversation_id,
            expected_version,
            state,
            events,
            worker_id=worker_id,
            fence=fence,
        )
        meta = self.interaction_meta.setdefault(interaction_id, {})
        meta["provider_correlation"] = provider_correlation or {}
        meta["request_event_sequence"] = request_event_sequence
        return committed

    async def commit_interaction_resolution(
        self,
        conversation_id: UUID,
        owner_id: str,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
        answer: InteractionAnswer,
        *,
        automatic: bool = False,
        create_rule: ApprovalRule | None = None,
        deciding_rule: ApprovalRule | None = None,
        provider_kind: str | None = None,
        provider_request_ids: dict[str, str] | None = None,
        resolution_event_sequence: int,
        mark_policy_evaluated: bool = False,
        interaction_id: UUID | None = None,
        suppress_answer_command: bool = False,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> InteractionResolutionResult:
        from uuid import uuid4

        iid = interaction_id or answer.interaction_id
        if worker_id is not None or fence is not None:
            self._require_owner(conversation_id, worker_id, fence)
        if iid in self.interaction_answers and not self.interaction_answers[iid].is_draft:
            return InteractionResolutionResult(
                answer=self.interaction_answers[iid],
                command=None,
                was_first_write=False,
                audit=None,
            )
        if automatic:
            current = self.states[conversation_id]
            interaction = current.interactions.get(iid)
            action = (
                interaction.request.action
                if interaction is not None
                and isinstance(interaction.request, ApprovalRequestPayload)
                else None
            )
            match = select_matching_rule(
                list(self.approval_rules.values()),
                action=action,
                ctx=InteractionMatchContext(
                    principal_id=owner_id,
                    conversation_id=conversation_id,
                    owner_id=owner_id,
                    binding=current.binding,
                    working_directory=(
                        current.binding.launch_snapshot.working_directory
                        if current.binding and current.binding.launch_snapshot
                        else None
                    ),
                ),
            )
            immediate = None
            if match.decision is ApprovalRuleDecision.ALLOW:
                immediate = ApprovalDecision.ALLOW_ONCE
            elif match.decision is ApprovalRuleDecision.DENY:
                immediate = ApprovalDecision.DENY
            available = (
                interaction.request.available_decisions
                if interaction is not None
                and isinstance(interaction.request, ApprovalRequestPayload)
                else ()
            )
            if immediate is None or immediate not in available or match.rule is None:
                self.interaction_meta.setdefault(iid, {})["policy_evaluated_at"] = (
                    current.conversation.updated_at
                )
                return InteractionResolutionResult(
                    answer=answer, command=None, was_first_write=False, audit=None
                )
            result = submit_interaction_answer(
                current,
                InteractionAnswer(interaction_id=iid, decision=immediate),
                now=answer.submitted_at or state.conversation.updated_at,
                automatic=True,
            )
            state = result.state
            events = result.events
            answer = state.answers[iid]
            resolution_event_sequence = events[-1].sequence
            deciding_rule = match.rule
            expected_version = current.conversation.version
        elif mark_policy_evaluated and not events:
            meta = self.interaction_meta.setdefault(iid, {})
            meta["policy_evaluated_at"] = state.conversation.updated_at
            return InteractionResolutionResult(
                answer=answer, command=None, was_first_write=False, audit=None
            )
        if events:
            await self.commit_turn_batch(
                conversation_id,
                expected_version,
                state,
                events,
                worker_id=worker_id,
                fence=fence,
            )
        if create_rule is not None:
            self.approval_rules[create_rule.id] = create_rule
            deciding_rule = create_rule
        self.interaction_answers[iid] = answer
        meta = self.interaction_meta.setdefault(iid, {})
        meta["resolution_event_sequence"] = resolution_event_sequence
        if suppress_answer_command:
            meta["answer_command_suppressed"] = True
        if mark_policy_evaluated or automatic:
            meta["policy_evaluated_at"] = answer.submitted_at or state.conversation.updated_at
        interaction = state.interactions.get(iid)
        raw_correlation: dict[str, str] = provider_request_ids or {}
        if provider_request_ids is None:
            stored_correlation = self.interaction_meta.get(iid, {}).get("provider_correlation", {})
            if isinstance(stored_correlation, dict):
                raw_correlation = {
                    str(key): str(value)
                    for key, value in cast(dict[object, object], stored_correlation).items()
                }
        audit = InteractionAuditProjection(
            id=uuid4(),
            principal_id=owner_id,
            interaction_id=iid,
            conversation_id=conversation_id,
            turn_id=interaction.turn_id if interaction else uuid4(),
            kind=interaction.kind if interaction else InteractionKind.APPROVAL,
            decision=answer.decision,
            answers=answer.answers,
            automatic=automatic,
            created_at=answer.submitted_at or state.conversation.updated_at,
            provider_kind=state.binding.kind if state.binding else None,
            provider_request_ids={str(key): str(value) for key, value in raw_correlation.items()},
            deciding_rule_id=deciding_rule.id if deciding_rule else None,
            rule_decision=deciding_rule.decision if deciding_rule else None,
            rule_scope=deciding_rule.scope if deciding_rule else None,
            rule_matcher=deciding_rule.matcher if deciding_rule else None,
        )
        self.interaction_audits[audit.id] = audit
        return InteractionResolutionResult(
            answer=answer, command=None, was_first_write=True, audit=audit
        )

    async def release_interaction_answer(
        self,
        conversation_id: UUID,
        owner_id: str,
        interaction_id: UUID,
        command: Command,
        *,
        expected_version: int,
        state: ConversationState,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> Command:
        if worker_id is not None or fence is not None:
            self._require_owner(conversation_id, worker_id, fence)
        meta = self.interaction_meta.setdefault(interaction_id, {})
        existing_id = meta.get("command_id")
        if isinstance(existing_id, UUID) and existing_id in self.commands:
            return self.commands[existing_id]
        self.commands[command.id] = command
        if command.status == CommandStatus.ACCEPTED and command.id not in self.accepted_queue:
            self.accepted_queue.append(command.id)
        meta["command_id"] = command.id
        meta["released_at"] = command.created_at
        current = self.states[conversation_id]
        commands = dict(current.commands)
        commands[command.id] = command
        self.states[conversation_id] = current.model_copy(update={"commands": commands})
        return command

    async def get_interaction_resolution_event(
        self,
        conversation_id: UUID,
        interaction_id: UUID,
    ) -> ConversationEvent:
        sequence = self.interaction_meta.get(interaction_id, {}).get("resolution_event_sequence")
        for event in self.events.get(conversation_id, ()):
            if (
                event.sequence == sequence
                and event.type == "interaction_resolved"
                and getattr(event.payload, "interaction_id", None) == interaction_id
            ):
                return event
        raise DomainError(ErrorCode.INVALID_STATE, "interaction resolution event missing")

    async def get_interaction_request_event(
        self,
        conversation_id: UUID,
        interaction_id: UUID,
    ) -> ConversationEvent:
        sequence = self.interaction_meta.get(interaction_id, {}).get("request_event_sequence")
        for event in self.events.get(conversation_id, ()):
            if (
                event.sequence == sequence
                and event.type == "interaction_requested"
                and getattr(event.payload, "interaction_id", None) == interaction_id
            ):
                return event
        raise DomainError(ErrorCode.INVALID_STATE, "interaction request event missing")

    async def complete_suppressed_interaction_resolution(
        self,
        interaction_id: UUID,
        published_at: datetime,
    ) -> bool:
        meta = self.interaction_meta.get(interaction_id)
        if meta is None:
            raise DomainError(ErrorCode.INVALID_STATE, "interaction answer missing")
        if meta.get("answer_command_suppressed") is not True:
            return False
        meta["released_at"] = published_at
        return True

    async def mark_interaction_policy_evaluated(
        self,
        interaction_id: UUID,
        evaluated_at: datetime,
    ) -> None:
        self.interaction_meta.setdefault(interaction_id, {})["policy_evaluated_at"] = evaluated_at

    async def list_unevaluated_open_interactions(self) -> Sequence[tuple[UUID, UUID]]:
        out: list[tuple[UUID, UUID]] = []
        for cid, interactions in self.interactions.items():
            for iid, interaction in interactions.items():
                if interaction.status not in {
                    InteractionStatus.PENDING,
                    InteractionStatus.DRAFT,
                }:
                    continue
                meta = self.interaction_meta.get(iid, {})
                if meta.get("policy_evaluated_at") is None:
                    out.append((cid, iid))
        return tuple(out)

    async def list_unreleased_resolutions(self) -> Sequence[tuple[UUID, UUID]]:
        out: list[tuple[UUID, UUID]] = []
        for iid, answer in self.interaction_answers.items():
            if answer.is_draft:
                continue
            meta = self.interaction_meta.get(iid, {})
            if meta.get("released_at") is None:
                # Find conversation id from states.
                for cid, state in self.states.items():
                    if iid in state.interactions:
                        out.append((cid, iid))
                        break
        return tuple(out)

    async def create_approval_rule(self, rule: ApprovalRule) -> ApprovalRuleProjection:
        self.approval_rules[rule.id] = rule
        return ApprovalRuleProjection(
            id=rule.id,
            principal_id=rule.principal_id,
            decision=rule.decision,
            scope=rule.scope,
            matcher=rule.matcher,
            created_at=rule.created_at,
            updated_at=rule.updated_at,
        )

    async def get_approval_rule(self, rule_id: UUID, principal_id: str) -> ApprovalRuleProjection:
        rule = self.approval_rules.get(rule_id)
        if rule is None or rule.principal_id != principal_id:
            raise _not_found("approval rule")
        return ApprovalRuleProjection(
            id=rule.id,
            principal_id=rule.principal_id,
            decision=rule.decision,
            scope=rule.scope,
            matcher=rule.matcher,
            created_at=rule.created_at,
            updated_at=rule.updated_at,
        )

    async def replace_approval_rule(self, rule: ApprovalRule) -> ApprovalRuleProjection:
        existing = self.approval_rules.get(rule.id)
        if existing is None or existing.principal_id != rule.principal_id:
            raise _not_found("approval rule")
        self.approval_rules[rule.id] = rule
        return ApprovalRuleProjection(
            id=rule.id,
            principal_id=rule.principal_id,
            decision=rule.decision,
            scope=rule.scope,
            matcher=rule.matcher,
            created_at=rule.created_at,
            updated_at=rule.updated_at,
        )

    async def delete_approval_rule(self, rule_id: UUID, principal_id: str) -> None:
        rule = self.approval_rules.get(rule_id)
        if rule is None or rule.principal_id != principal_id:
            raise _not_found("approval rule")
        del self.approval_rules[rule_id]

    async def page_approval_rules(
        self,
        principal_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[ApprovalRuleProjection]:
        from talktoharnesses.application.cursors import clamp_page_limit, encode_cursor

        limit = clamp_page_limit(limit)
        rules = sorted(
            (r for r in self.approval_rules.values() if r.principal_id == principal_id),
            key=lambda r: (r.created_at, r.id),
            reverse=True,
        )
        items = [
            ApprovalRuleProjection(
                id=r.id,
                principal_id=r.principal_id,
                decision=r.decision,
                scope=r.scope,
                matcher=r.matcher,
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
            for r in rules[:limit]
        ]
        next_cursor = None
        if len(rules) > limit:
            last = items[-1]
            next_cursor = encode_cursor(sort=last.created_at.isoformat(), id=last.id)
        return Page(items=tuple(items), next_cursor=next_cursor)

    async def list_applicable_approval_rules(self, principal_id: str) -> Sequence[ApprovalRule]:
        return tuple(r for r in self.approval_rules.values() if r.principal_id == principal_id)

    async def get_interaction_audit(
        self, audit_id: UUID, principal_id: str
    ) -> InteractionAuditProjection:
        audit = self.interaction_audits.get(audit_id)
        if audit is None or audit.principal_id != principal_id:
            raise _not_found("interaction audit")
        return audit

    async def page_interaction_audits(
        self,
        principal_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[InteractionAuditProjection]:
        from talktoharnesses.application.cursors import clamp_page_limit, encode_cursor

        limit = clamp_page_limit(limit)
        audits = sorted(
            (a for a in self.interaction_audits.values() if a.principal_id == principal_id),
            key=lambda a: (a.created_at, a.id),
            reverse=True,
        )
        items = audits[:limit]
        next_cursor = None
        if len(audits) > limit:
            last = items[-1]
            next_cursor = encode_cursor(sort=last.created_at.isoformat(), id=last.id)
        return Page(items=tuple(items), next_cursor=next_cursor)

    async def read_retained_handoff(
        self,
        conversation_id: UUID,
        *,
        owner_id: str | None = None,
    ) -> HandoffDocument:
        if owner_id is not None:
            await self._require_owned(conversation_id, owner_id)
        turn_order = {
            turn_id: index
            for index, turn_id in enumerate(self.turn_order.get(conversation_id, []), start=1)
        }
        item_order = self.item_order_index.get(conversation_id, {})
        entries: list[HandoffMessage | HandoffTool] = []
        for index, message in enumerate(self.messages.get(conversation_id, {}).values(), start=1):
            entries.append(
                HandoffMessage(
                    id=message.id,
                    turn_id=message.turn_id,
                    role=message.role,
                    text=message.text,
                    interrupted=message.interrupted,
                    turn_order_index=turn_order.get(message.turn_id, 0),
                    order_index=item_order.get(message.id, index),
                )
            )
        for index, tool in enumerate(self.tools.get(conversation_id, {}).values(), start=1):
            entries.append(
                HandoffTool(
                    **tool.model_dump(exclude={"full_output"}),
                    turn_order_index=turn_order.get(tool.turn_id, 0),
                    order_index=item_order.get(tool.id, index),
                )
            )
        return HandoffDocument(entries=tuple(sorted(entries, key=handoff_sort_key)))

    async def read_retained_export(
        self,
        conversation_id: UUID,
        owner_id: str,
    ) -> tuple[HandoffDocument, str]:
        state = await self._require_owned(conversation_id, owner_id)
        handoff = await self.read_retained_handoff(conversation_id)
        return handoff, state.conversation.display_title

    async def commit_transcript_import(
        self,
        state: ConversationState,
        handoff: HandoffDocument,
        events: Sequence[ConversationEvent],
        *,
        process: ProcessRecord | None = None,
        launch_history_entry: LaunchSnapshot | None = None,
    ) -> Sequence[ConversationEvent]:
        cid = state.conversation.id
        if cid in self.states:
            raise DomainError(ErrorCode.INVALID_STATE, "conversation already exists")
        await self.save_snapshot(state)
        turns = self.turns.setdefault(cid, {})
        order = self.turn_order.setdefault(cid, [])
        messages = self.messages.setdefault(cid, {})
        tools = self.tools.setdefault(cid, {})
        item_order = self.item_order_index.setdefault(cid, {})
        grouped: dict[UUID, list[HandoffMessage | HandoffTool]] = {}
        turn_ids: list[UUID] = []
        for entry in sorted(handoff.entries, key=handoff_sort_key):
            if entry.turn_id not in grouped:
                grouped[entry.turn_id] = []
                turn_ids.append(entry.turn_id)
            grouped[entry.turn_id].append(entry)
        for turn_id in turn_ids:
            entries = grouped[turn_id]
            user_message_id = next(
                (
                    entry.id
                    for entry in entries
                    if isinstance(entry, HandoffMessage) and entry.role is MessageRole.USER
                ),
                None,
            )
            turns[turn_id] = Turn(
                id=turn_id,
                conversation_id=cid,
                status=TurnStatus.COMPLETED,
                user_message_id=user_message_id,
                created_at=state.conversation.created_at,
                started_at=state.conversation.created_at,
                completed_at=state.conversation.created_at,
            )
            if turn_id not in order:
                order.append(turn_id)
            for entry in entries:
                item_order[entry.id] = entry.order_index
                if isinstance(entry, HandoffMessage):
                    messages[entry.id] = Message(
                        id=entry.id,
                        turn_id=turn_id,
                        role=entry.role,
                        text=entry.text,
                        interrupted=entry.interrupted,
                        completed=True,
                        created_at=state.conversation.created_at,
                    )
                else:
                    tools[entry.id] = CanonicalToolResult(
                        id=entry.id,
                        turn_id=turn_id,
                        tool_name=entry.tool_name,
                        arguments=dict(entry.arguments),
                        outcome=entry.outcome,
                        exit_status=entry.exit_status,
                        paths=entry.paths,
                        output_tail=entry.output_tail,
                    )
        event_list = self.events.setdefault(cid, [])
        event_list.extend(events)
        if process is not None:
            self.processes[process.id] = process
        if launch_history_entry is not None:
            self.launch_history.setdefault(cid, []).append(launch_history_entry)
        self._refresh_search(state)
        return tuple(events)

    async def prepare_harness_switch(self, conversation_id: UUID) -> SwitchPreparation:
        state = self.states.get(conversation_id)
        if state is None:
            raise _not_found()
        return SwitchPreparation(
            state=state,
            handoff=await self.read_retained_handoff(conversation_id),
        )

    async def commit_harness_switch(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
        *,
        command: Command,
        process: ProcessRecord | None = None,
        launch_history_entry: LaunchSnapshot | None = None,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> Sequence[ConversationEvent]:
        committed = await self.commit_runtime_lifecycle(
            conversation_id,
            expected_version,
            state,
            process,
            launch_history_entry,
            events,
            worker_id=worker_id,
            fence=fence,
        )
        self.commands[command.id] = command
        return committed

    async def commit_harness_switch_failure(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
        *,
        command: Command,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> Sequence[ConversationEvent]:
        return await self.commit_turn_batch(
            conversation_id,
            expected_version,
            state,
            events,
            (command,),
            worker_id=worker_id,
            fence=fence,
        )

    async def get_retention_policy(self, owner_id: str) -> RetentionPolicyProjection:
        from talktoharnesses.application.retention import DEFAULT_RETENTION_MONTHS

        return self.retention_policies.get(
            owner_id,
            RetentionPolicyProjection(months=DEFAULT_RETENTION_MONTHS, updated_at=None),
        )

    async def replace_retention_policy(
        self,
        owner_id: str,
        months: int,
        *,
        now: datetime,
    ) -> RetentionPolicyProjection:
        policy = RetentionPolicyProjection(months=months, updated_at=now)
        self.retention_policies[owner_id] = policy
        return policy

    async def preview_retention(
        self,
        owner_id: str,
        *,
        now: datetime,
    ) -> RetentionPreviewProjection:
        from talktoharnesses.application.retention import (
            classify_history_eligibility,
            is_terminal_turn_expired,
            months_before,
            soft_delete_purge_eligible,
        )

        policy = await self.get_retention_policy(owner_id)
        cutoff = months_before(now, policy.months)
        soft_deleted = 0
        history_conversations = 0
        terminal_turns = 0
        waiting_turns = 0
        for state in self.states.values():
            if state.conversation.owner_id != owner_id:
                continue
            if soft_delete_purge_eligible(state.conversation.deleted_at, cutoff):
                soft_deleted += 1
                continue
            if state.conversation.deleted_at is not None:
                continue
            eligibility = classify_history_eligibility(state, cutoff)
            if eligibility.blocked:
                continue
            turns = self.turns.get(state.conversation.id, {})
            terminal = sum(
                1
                for turn in turns.values()
                if is_terminal_turn_expired(turn.status, turn.completed_at, cutoff)
            )
            waiting = 1 if eligibility.waiting_expired else 0
            if terminal == 0 and waiting == 0:
                continue
            history_conversations += 1
            terminal_turns += terminal
            waiting_turns += waiting
        return RetentionPreviewProjection(
            cutoff=cutoff,
            soft_deleted_conversations=soft_deleted,
            history_conversations=history_conversations,
            terminal_turns=terminal_turns,
            waiting_turns=waiting_turns,
        )

    async def list_retention_owner_ids(self) -> Sequence[str]:
        return sorted({state.conversation.owner_id for state in self.states.values()})

    async def list_cleanup_conversation_ids(self) -> Sequence[tuple[UUID, str]]:
        return [
            (cid, state.conversation.owner_id)
            for cid, state in self.states.items()
            if state.conversation.deleted_at is None
        ]

    async def prune_expired_history(
        self,
        conversation_id: UUID,
        cutoff: datetime,
    ) -> PruneResult | None:
        from talktoharnesses.application.retention import (
            classify_history_eligibility,
            is_terminal_turn_expired,
        )

        state = self.states.get(conversation_id)
        if state is None:
            return None
        eligibility = classify_history_eligibility(state, cutoff)
        if eligibility.blocked:
            return None
        waiting_expired = eligibility.waiting_expired
        active = state.active_turn

        turns = self.turns.get(conversation_id, {})
        expired = {
            turn_id
            for turn_id, turn in turns.items()
            if is_terminal_turn_expired(turn.status, turn.completed_at, cutoff)
        }
        if active is not None and waiting_expired:
            expired.add(active.id)
        if not expired:
            return None

        now = datetime.now(UTC)
        events: tuple[ConversationEvent, ...] = ()
        if waiting_expired:
            interrupted = interrupt_turn(state, now=now, reason="retention")
            state = interrupted.state
            events += interrupted.events

        removed_interactions = {
            interaction_id
            for interaction_id, interaction in self.interactions.get(conversation_id, {}).items()
            if interaction.turn_id in expired
        }
        self.turns[conversation_id] = {
            turn_id: turn for turn_id, turn in turns.items() if turn_id not in expired
        }
        self.turn_order[conversation_id] = [
            turn_id
            for turn_id in self.turn_order.get(conversation_id, [])
            if turn_id not in expired
        ]
        self.messages[conversation_id] = {
            message_id: message
            for message_id, message in self.messages.get(conversation_id, {}).items()
            if message.turn_id not in expired
        }
        self.tools[conversation_id] = {
            tool_id: tool
            for tool_id, tool in self.tools.get(conversation_id, {}).items()
            if tool.turn_id not in expired
        }
        self.plans[conversation_id] = {
            plan_id: plan
            for plan_id, plan in self.plans.get(conversation_id, {}).items()
            if plan.turn_id not in expired
        }
        self.activities[conversation_id] = {
            activity_id: activity
            for activity_id, activity in self.activities.get(conversation_id, {}).items()
            if activity.parent_turn_id not in expired
        }
        self.interactions[conversation_id] = {
            interaction_id: interaction
            for interaction_id, interaction in self.interactions.get(conversation_id, {}).items()
            if interaction_id not in removed_interactions
        }
        for interaction_id in removed_interactions:
            self.interaction_answers.pop(interaction_id, None)
            self.interaction_meta.pop(interaction_id, None)
        for command_id in [
            command_id
            for command_id, command in self.commands.items()
            if command.conversation_id == conversation_id and command.target_turn_id in expired
        ]:
            del self.commands[command_id]
        interaction_turn_ids = {
            interaction_id: interaction.turn_id
            for interaction_id, interaction in state.interactions.items()
        }
        self.events[conversation_id] = [
            event
            for event in self.events.get(conversation_id, [])
            if event_turn_id(event, interaction_turn_ids) not in expired
        ]

        state = state.model_copy(
            update={
                "commands": {
                    command_id: command
                    for command_id, command in state.commands.items()
                    if command.target_turn_id not in expired
                },
                "interactions": {
                    interaction_id: interaction
                    for interaction_id, interaction in state.interactions.items()
                    if interaction_id not in removed_interactions
                },
                "answers": {
                    interaction_id: answer
                    for interaction_id, answer in state.answers.items()
                    if interaction_id not in removed_interactions
                },
                "activities": {
                    activity_id: activity
                    for activity_id, activity in state.activities.items()
                    if activity.parent_turn_id not in expired
                },
            }
        )
        binding = state.binding
        assert binding is not None
        previous_native_session_id = binding.native_session_id
        rotated = rotate_session(state, now=now)
        state = rotated.state
        events += rotated.events

        self.states[conversation_id] = state
        self.events[conversation_id].extend(
            event for event in events if event_turn_id(event) not in expired
        )
        self._refresh_search(state)
        return PruneResult(
            conversation_id=conversation_id,
            owner_id=state.conversation.owner_id,
            binding_id=binding.id,
            previous_native_session_id=previous_native_session_id,
            configuration=binding.configuration,
            handoff=await self.read_retained_handoff(conversation_id),
            version=state.conversation.version,
            session_rotated_events=events,
            pruned_turn_count=len(expired),
            cancelled_waiting_count=1 if waiting_expired else 0,
        )

    async def commit_session_rotation(
        self,
        conversation_id: UUID,
        expected_version: int,
        *,
        native_session_id: str | None,
        launch_snapshot: LaunchSnapshot | None,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> None:
        if worker_id is not None or fence is not None:
            self._require_owner(conversation_id, worker_id, fence)
        state = self._require_binding(conversation_id, expected_version)
        assert state.binding is not None
        binding = state.binding.model_copy(
            update={
                "native_session_id": native_session_id,
                "launch_snapshot": launch_snapshot or state.binding.launch_snapshot,
                "requires_session_recreation": False,
            }
        )
        self.states[conversation_id] = state.model_copy(
            update={
                "binding": binding,
                "seen_native_ids": frozenset(),
                "seen_stream_offsets": frozenset(),
            }
        )

    async def commit_rotation_requires_recreation(
        self,
        conversation_id: UUID,
        expected_version: int,
        *,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> None:
        if worker_id is not None or fence is not None:
            self._require_owner(conversation_id, worker_id, fence)
        state = self._require_binding(conversation_id, expected_version)
        self.states[conversation_id] = mark_requires_recreation(state, now=datetime.now(UTC)).state

    async def acquire_worker_lease(
        self,
        worker_id: str,
        *,
        lease_duration: float,
        slot: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        lease_slot = _SQLITE_SUPERVISOR_SLOT if self._sqlite_mode else (slot or worker_id)
        expires = now + timedelta(seconds=lease_duration)
        existing = self.worker_leases.get(lease_slot)
        if existing is not None:
            existing_expires = cast(datetime, existing["expires_at"])
            existing_worker = cast(str, existing["worker_id"])
            if existing_expires >= now and existing_worker != worker_id:
                raise DomainError(
                    ErrorCode.WORKER_LEASE_UNAVAILABLE,
                    "worker lease unavailable",
                    details={"slot": lease_slot},
                )
            if existing_worker == worker_id and existing_expires >= now:
                existing["heartbeat_at"] = now
                existing["expires_at"] = expires
                existing["draining"] = False
                return
        self.worker_leases[lease_slot] = {
            "worker_id": worker_id,
            "started_at": now,
            "heartbeat_at": now,
            "expires_at": expires,
            "draining": False,
        }

    async def renew_worker_lease(self, worker_id: str, *, lease_duration: float) -> None:
        now = datetime.now(UTC)
        lease_slot = _SQLITE_SUPERVISOR_SLOT if self._sqlite_mode else worker_id
        existing = self.worker_leases.get(lease_slot)
        if (
            existing is None
            or cast(str, existing["worker_id"]) != worker_id
            or cast(datetime, existing["expires_at"]) < now
        ):
            raise DomainError(
                ErrorCode.WORKER_LEASE_UNAVAILABLE,
                "worker lease unavailable",
                details={"slot": lease_slot},
            )
        existing["heartbeat_at"] = now
        existing["expires_at"] = now + timedelta(seconds=lease_duration)

    async def mark_worker_draining(self, worker_id: str) -> None:
        lease_slot = _SQLITE_SUPERVISOR_SLOT if self._sqlite_mode else worker_id
        existing = self.worker_leases.get(lease_slot)
        if existing is None or cast(str, existing["worker_id"]) != worker_id:
            raise DomainError(
                ErrorCode.WORKER_LEASE_UNAVAILABLE,
                "worker lease unavailable",
                details={"slot": lease_slot},
            )
        existing["draining"] = True

    async def release_worker_lease(self, worker_id: str) -> None:
        lease_slot = _SQLITE_SUPERVISOR_SLOT if self._sqlite_mode else worker_id
        existing = self.worker_leases.get(lease_slot)
        if existing is not None and cast(str, existing["worker_id"]) == worker_id:
            del self.worker_leases[lease_slot]

    async def claim_expired_conversations(
        self,
        worker_id: str,
        limit: int,
        *,
        lease_duration: float,
        trigger: str = RecoveryTrigger.TAKEOVER.value,
    ) -> Sequence[ConversationOwnership]:
        now = datetime.now(UTC)
        lease_expires = now + timedelta(seconds=lease_duration)
        claimed: list[ConversationOwnership] = []
        for conversation_id, state in sorted(self.states.items(), key=lambda item: item[0]):
            if len(claimed) >= limit:
                break
            if state.conversation.deleted_at is not None:
                continue
            ownership = self.ownership.get(conversation_id)
            lease_expired = ownership is not None and ownership[2] < now
            unowned_active = ownership is None and (
                state.conversation.status in _ACTIVE_RECOVERY_STATUSES
            )
            expired_command_owner = any(
                command.status
                in {
                    CommandStatus.CLAIMED,
                    CommandStatus.DELIVERY_STARTED,
                    CommandStatus.DELIVERED,
                }
                and (command.lease_expires_at is None or command.lease_expires_at < now)
                for command in state.commands.values()
            )
            if not lease_expired and not unowned_active and not expired_command_owner:
                continue
            if ownership is not None and ownership[0] == worker_id and ownership[2] >= now:
                continue
            fence = (ownership[1] if ownership is not None else 0) + 1
            self._mark_processes_orphaned(conversation_id, now=now)
            for attempt_id, attempt in list(self.recovery_attempts.items()):
                if attempt.conversation_id == conversation_id and attempt.result is None:
                    self.recovery_attempts[attempt_id] = RecoveryAttempt(
                        id=attempt.id,
                        conversation_id=attempt.conversation_id,
                        binding_id=attempt.binding_id,
                        command_id=attempt.command_id,
                        turn_id=attempt.turn_id,
                        worker_id=attempt.worker_id,
                        fence=attempt.fence,
                        trigger=attempt.trigger,
                        observed_delivery_phase=attempt.observed_delivery_phase,
                        action=attempt.action,
                        result=RecoveryResultCode.ABANDONED.value,
                        reason_code=RecoveryReasonCode.WORKER_LOST.value,
                        started_at=attempt.started_at,
                        completed_at=now,
                    )
            binding_id = (
                state.binding.id
                if state.binding is not None
                else state.conversation.current_binding_id
            )
            if binding_id is None:
                raise DomainError(
                    ErrorCode.INVALID_STATE,
                    "active conversation missing binding for recovery",
                    details={"conversation_id": str(conversation_id)},
                )
            attempt_id = uuid4()
            self.recovery_attempts[attempt_id] = RecoveryAttempt(
                id=attempt_id,
                conversation_id=conversation_id,
                binding_id=binding_id,
                command_id=None,
                turn_id=None,
                worker_id=worker_id,
                fence=fence,
                trigger=trigger,
                observed_delivery_phase=ObservedDeliveryPhase.NONE.value,
                action=RecoveryAction.NO_ACTION.value,
                result=None,
                reason_code=RecoveryReasonCode.WORKER_LOST.value,
                started_at=now,
                completed_at=None,
            )
            self.ownership[conversation_id] = (worker_id, fence, lease_expires)
            claimed.append(
                ConversationOwnership(
                    conversation_id=conversation_id,
                    worker_id=worker_id,
                    fence=fence,
                    lease_expires_at=lease_expires,
                    recovery_attempt_id=attempt_id,
                )
            )
        return tuple(claimed)

    async def renew_owned_conversation_leases(
        self,
        worker_id: str,
        *,
        lease_duration: float,
    ) -> Sequence[LostLease]:
        now = datetime.now(UTC)
        lease_expires = now + timedelta(seconds=lease_duration)
        lost: list[LostLease] = []
        for conversation_id, (owner, fence, expires_at) in list(self.ownership.items()):
            if owner != worker_id:
                continue
            if expires_at < now:
                lost.append(LostLease(conversation_id=conversation_id, fence=fence))
                del self.ownership[conversation_id]
                continue
            self.ownership[conversation_id] = (worker_id, fence, lease_expires)
        return tuple(lost)

    async def release_conversation_lease(
        self,
        conversation_id: UUID,
        worker_id: str,
        fence: int,
    ) -> None:
        self._require_owner(conversation_id, worker_id, fence)
        self.ownership.pop(conversation_id, None)

    async def complete_recovery_attempt(
        self,
        attempt_id: UUID,
        *,
        result: str,
        reason_code: str,
        completed_at: datetime,
    ) -> None:
        attempt = self.recovery_attempts.get(attempt_id)
        if attempt is None or attempt.result is not None:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "recovery attempt not found or already completed",
                details={"attempt_id": str(attempt_id)},
            )
        self.recovery_attempts[attempt_id] = RecoveryAttempt(
            id=attempt.id,
            conversation_id=attempt.conversation_id,
            binding_id=attempt.binding_id,
            command_id=attempt.command_id,
            turn_id=attempt.turn_id,
            worker_id=attempt.worker_id,
            fence=attempt.fence,
            trigger=attempt.trigger,
            observed_delivery_phase=attempt.observed_delivery_phase,
            action=attempt.action,
            result=result,
            reason_code=reason_code,
            started_at=attempt.started_at,
            completed_at=completed_at,
        )

    async def commit_recovery_batch(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
        commands: Sequence[Command],
        *,
        interrupted_turn_id: UUID | None,
        attempt_id: UUID | None,
        command_id: UUID | None,
        turn_id: UUID | None,
        trigger: str,
        observed_delivery_phase: str,
        action: str,
        result: str,
        reason_code: str,
        completed_at: datetime,
        worker_id: str,
        fence: int,
    ) -> Sequence[ConversationEvent]:
        self._require_owner(conversation_id, worker_id, fence)
        previous = self.states[conversation_id]
        for interaction_id, answer in state.answers.items():
            interaction = state.interactions.get(interaction_id)
            if (
                interaction is None
                or interaction.status is not InteractionStatus.CANCELLED
                or interaction_id in previous.answers
            ):
                continue
            sequence = next(
                event.sequence
                for event in events
                if event.type == "interaction_resolved"
                and getattr(event.payload, "interaction_id", None) == interaction_id
            )
            await self.commit_interaction_resolution(
                conversation_id,
                state.conversation.owner_id,
                expected_version,
                state,
                (),
                answer,
                resolution_event_sequence=sequence,
                suppress_answer_command=True,
                worker_id=worker_id,
                fence=fence,
            )
        committed = await self.commit_turn_batch(
            conversation_id,
            expected_version,
            state,
            events,
            commands,
            worker_id=worker_id,
            fence=fence,
        )
        if interrupted_turn_id is not None:
            await self.mark_incomplete_assistant_messages_interrupted(
                conversation_id, interrupted_turn_id
            )
        if attempt_id is not None:
            await self.update_recovery_attempt(
                attempt_id,
                command_id=command_id,
                turn_id=turn_id,
                trigger=trigger,
                observed_delivery_phase=observed_delivery_phase,
                action=action,
                reason_code=reason_code,
                worker_id=worker_id,
                fence=fence,
            )
            await self.complete_recovery_attempt(
                attempt_id,
                result=result,
                reason_code=reason_code,
                completed_at=completed_at,
            )
        return committed

    async def get_open_recovery_attempt(
        self,
        conversation_id: UUID,
        worker_id: str,
        fence: int,
    ) -> RecoveryAttempt | None:
        open_attempts = [
            attempt
            for attempt in self.recovery_attempts.values()
            if (
                attempt.conversation_id == conversation_id
                and attempt.worker_id == worker_id
                and attempt.fence == fence
                and attempt.result is None
            )
        ]
        if not open_attempts:
            return None
        open_attempts.sort(key=lambda item: item.started_at, reverse=True)
        return open_attempts[0]

    async def update_recovery_attempt(
        self,
        attempt_id: UUID,
        *,
        command_id: UUID | None,
        turn_id: UUID | None,
        trigger: str,
        observed_delivery_phase: str,
        action: str,
        reason_code: str,
        worker_id: str,
        fence: int,
    ) -> None:
        attempt = self.recovery_attempts[attempt_id]
        self._require_owner(attempt.conversation_id, worker_id, fence)
        if attempt.worker_id != worker_id or attempt.fence != fence or attempt.result is not None:
            raise DomainError(ErrorCode.STALE_OWNER, "stale conversation owner")
        self.recovery_attempts[attempt_id] = RecoveryAttempt(
            id=attempt.id,
            conversation_id=attempt.conversation_id,
            binding_id=attempt.binding_id,
            command_id=command_id,
            turn_id=turn_id,
            worker_id=attempt.worker_id,
            fence=attempt.fence,
            trigger=trigger,
            observed_delivery_phase=observed_delivery_phase,
            action=action,
            result=None,
            reason_code=reason_code,
            started_at=attempt.started_at,
            completed_at=None,
        )

    async def mark_incomplete_assistant_messages_interrupted(
        self,
        conversation_id: UUID,
        turn_id: UUID,
    ) -> None:
        messages = self.messages.get(conversation_id, {})
        for message_id, message in list(messages.items()):
            if (
                message.turn_id == turn_id
                and message.role is MessageRole.ASSISTANT
                and not message.completed
            ):
                messages[message_id] = message.model_copy(update={"interrupted": True})

    def _require_owner(
        self,
        conversation_id: UUID,
        worker_id: str | None,
        fence: int | None,
    ) -> None:
        if worker_id is None and fence is None:
            return
        if worker_id is None or fence is None:
            raise _stale_owner(conversation_id=conversation_id)
        ownership = self.ownership.get(conversation_id)
        now = datetime.now(UTC)
        if (
            ownership is None
            or ownership[0] != worker_id
            or ownership[1] != fence
            or ownership[2] < now
        ):
            raise _stale_owner(conversation_id=conversation_id)

    def _acquire_conversation_owner(
        self,
        conversation_id: UUID,
        worker_id: str,
        *,
        lease_duration: float,
        now: datetime,
    ) -> int | None:
        lease_expires = now + timedelta(seconds=lease_duration)
        ownership = self.ownership.get(conversation_id)
        if ownership is not None and ownership[0] != worker_id and ownership[2] >= now:
            return None
        if ownership is not None and ownership[0] == worker_id and ownership[2] >= now:
            fence = ownership[1]
        else:
            fence = (ownership[1] if ownership is not None else 0) + 1
            if ownership is not None and ownership[0] != worker_id:
                self._mark_processes_orphaned(conversation_id, now=now)
        self.ownership[conversation_id] = (worker_id, fence, lease_expires)
        return fence

    def _mark_processes_orphaned(self, conversation_id: UUID, *, now: datetime) -> None:
        for process_id, process in list(self.processes.items()):
            if process.conversation_id != conversation_id:
                continue
            if process.status in {ProcessStatus.STARTING, ProcessStatus.RUNNING}:
                self.processes[process_id] = process.model_copy(
                    update={"status": ProcessStatus.ORPHANED, "orphaned_at": now}
                )

    def _require_binding(self, conversation_id: UUID, expected_version: int) -> ConversationState:
        state = self.states.get(conversation_id)
        if state is None:
            raise _not_found("conversation")
        if state.conversation.version != expected_version:
            raise DomainError(
                ErrorCode.OPTIMISTIC_CONFLICT,
                "optimistic concurrency conflict",
                details={
                    "expected": expected_version,
                    "actual": state.conversation.version,
                },
            )
        if state.binding is None:
            raise DomainError(ErrorCode.INVALID_STATE, "no binding to rotate")
        return state

    async def purge_soft_deleted(self, now: datetime) -> int:
        from talktoharnesses.application.retention import months_before, soft_delete_purge_eligible

        to_delete: list[UUID] = []
        for cid, state in self.states.items():
            if state.conversation.deleted_at is None:
                continue
            policy = await self.get_retention_policy(state.conversation.owner_id)
            cutoff = months_before(now, policy.months)
            if soft_delete_purge_eligible(state.conversation.deleted_at, cutoff):
                to_delete.append(cid)
        for cid in to_delete:
            del self.states[cid]
            self.events.pop(cid, None)
            self.search_docs.pop(cid, None)
        return len(to_delete)

    # ------------------------------------------------------------------
    # Phase 5 facade surface
    # ------------------------------------------------------------------

    async def create_harness(self, harness: HarnessInstance) -> HarnessProjection:
        self.harnesses[harness.id] = harness
        return self._harness_proj(harness)

    async def list_harnesses(
        self,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[HarnessProjection]:
        page_size = clamp_page_limit(limit)
        items = [h for h in self.harnesses.values() if h.owner_id == owner_id]
        items.sort(key=lambda h: (h.created_at, h.id), reverse=True)
        if cursor is not None:
            sort, item_id = decode_cursor(cursor)
            sort_dt = datetime.fromisoformat(sort)
            items = [
                h
                for h in items
                if h.created_at < sort_dt or (h.created_at == sort_dt and h.id < item_id)
            ]
        page = items[:page_size]
        next_cursor = None
        if len(items) > page_size and page:
            last = page[-1]
            next_cursor = encode_cursor(sort=last.created_at.isoformat(), id=last.id)
        return Page(items=tuple(self._harness_proj(h) for h in page), next_cursor=next_cursor)

    async def get_harness(self, harness_id: UUID, owner_id: str) -> HarnessProjection:
        harness = self.harnesses.get(harness_id)
        if harness is None or harness.owner_id != owner_id:
            raise _not_found("harness")
        return self._harness_proj(harness)

    async def delete_harness(self, harness_id: UUID, owner_id: str) -> None:
        harness = self.harnesses.get(harness_id)
        if harness is None or harness.owner_id != owner_id:
            raise _not_found("harness")
        if any(
            state.binding is not None
            and state.binding.is_active
            and state.binding.harness_instance_id == harness_id
            and state.active_turn is not None
            for state in self.states.values()
        ):
            raise DomainError(ErrorCode.HARNESS_IN_USE, "harness has an active turn")
        del self.harnesses[harness_id]
        self.harness_probes.pop(harness_id, None)

    async def save_harness_probe(
        self,
        harness_id: UUID,
        owner_id: str,
        capabilities: HarnessCapabilities,
        *,
        probed_at: datetime,
    ) -> HarnessProbeProjection:
        harness = self.harnesses.get(harness_id)
        if harness is None or harness.owner_id != owner_id:
            raise _not_found("harness")
        self.harness_probes[harness_id] = (capabilities, probed_at)
        return HarnessProbeProjection(
            harness_id=harness_id, capabilities=capabilities, probed_at=probed_at
        )

    async def get_harness_probe(
        self,
        harness_id: UUID,
        owner_id: str,
    ) -> HarnessProbeProjection:
        harness = self.harnesses.get(harness_id)
        if harness is None or harness.owner_id != owner_id:
            raise _not_found("harness")
        probe = self.harness_probes.get(harness_id)
        if probe is None:
            raise _not_found("harness probe")
        caps, probed_at = probe
        return HarnessProbeProjection(harness_id=harness_id, capabilities=caps, probed_at=probed_at)

    async def list_configured_harnesses_for_readiness(self) -> Sequence[HarnessProjection]:
        items = sorted(self.harnesses.values(), key=lambda h: h.id)
        return tuple(self._harness_proj(h) for h in items)

    async def has_fresh_harness_probe(
        self,
        *,
        now: datetime,
        max_age_seconds: int = 300,
    ) -> bool:
        cutoff = now - timedelta(seconds=max_age_seconds)
        return any(probed_at > cutoff for _caps, probed_at in self.harness_probes.values())

    async def list_conversations(
        self,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
        include_archived: bool = True,
    ) -> Page[ConversationShell]:
        page_size = clamp_page_limit(limit)
        shells = [
            self._shell(s)
            for s in self.states.values()
            if s.conversation.owner_id == owner_id and s.conversation.deleted_at is None
        ]
        if not include_archived:
            shells = [s for s in shells if s.archived_at is None]
        shells.sort(key=lambda s: (s.updated_at, s.id), reverse=True)
        if cursor is not None:
            sort, item_id = decode_cursor(cursor)
            sort_dt = datetime.fromisoformat(sort)
            shells = [
                s
                for s in shells
                if s.updated_at < sort_dt or (s.updated_at == sort_dt and s.id < item_id)
            ]
        page = shells[:page_size]
        next_cursor = None
        if len(shells) > page_size and page:
            last = page[-1]
            next_cursor = encode_cursor(sort=last.updated_at.isoformat(), id=last.id)
        return Page(items=tuple(page), next_cursor=next_cursor)

    async def search_conversations(
        self,
        owner_id: str,
        query: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[ConversationSearchHit]:
        page_size = clamp_page_limit(limit)
        parsed = parse_search_query(query)
        ranked: list[tuple[int, ConversationState, SearchDocumentFields]] = []
        for cid, doc in self.search_docs.items():
            state = self.states.get(cid)
            if state is None or state.conversation.owner_id != owner_id:
                continue
            conversation = state.conversation
            if conversation.deleted_at is not None:
                continue
            filters = parsed.filters
            if filters.pinned is True and conversation.pinned_at is None:
                continue
            if filters.archived is True and conversation.archived_at is None:
                continue
            if filters.has_interaction is True:
                pending = any(
                    i.status in {InteractionStatus.PENDING, InteractionStatus.DRAFT}
                    for i in state.interactions.values()
                )
                if not pending:
                    continue
            if filters.harness is not None and (
                state.binding is None or state.binding.kind != filters.harness
            ):
                continue
            if filters.before is not None and not (conversation.updated_at < filters.before):
                continue
            if filters.after is not None and not (conversation.updated_at >= filters.after):
                continue
            if not all(
                count_token_occurrences(doc.normalized_text, clause.normalized)
                for clause in parsed.positive
            ):
                continue
            if document_matches_exclusions(parsed, normalized_text=doc.normalized_text):
                continue
            score = rank_document(
                parsed, search_title=doc.search_title, search_body=doc.search_body
            )
            ranked.append((score, state, doc))
        ranked.sort(
            key=lambda item: (item[0], item[1].conversation.updated_at, item[1].conversation.id),
            reverse=True,
        )
        if cursor is not None:
            cursor_rank, cursor_updated, cursor_id = decode_search_cursor(
                cursor, digest=parsed.digest
            )
            ranked = [
                item
                for item in ranked
                if (
                    item[0],
                    item[1].conversation.updated_at,
                    item[1].conversation.id,
                )
                < (cursor_rank, cursor_updated, cursor_id)
            ]
        page = ranked[:page_size]
        next_cursor = None
        if len(ranked) > page_size and page:
            last_score, last_state, _ = page[-1]
            next_cursor = encode_search_cursor(
                rank=last_score,
                updated_at=last_state.conversation.updated_at.isoformat(),
                id=last_state.conversation.id,
                digest=parsed.digest,
            )
        return Page(
            items=tuple(
                ConversationSearchHit(
                    conversation=self._shell(state),
                    snippet=build_snippet(parsed, doc.snippet_text),
                )
                for _score, state, doc in page
            ),
            next_cursor=next_cursor,
        )

    async def get_conversation_snapshot(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        include_deleted: bool = False,
    ) -> ConversationSnapshot:
        state = await self._require_owned(
            conversation_id,
            owner_id,
            include_deleted=include_deleted,
        )
        high_water = max(0, state.conversation.next_event_sequence - 1)
        turns = list(self.turns.get(conversation_id, {}).values())
        user_turns = [t for t in turns if t.user_message_id is not None]
        order = {tid: i for i, tid in enumerate(self.turn_order.get(conversation_id, []))}
        user_turns.sort(key=lambda t: order.get(t.id, 0), reverse=True)
        selected = list(reversed(user_turns[:20]))
        turn_projs = tuple(self._turn_proj(t) for t in selected)
        selected_ids = {turn.id for turn in selected}
        selected_messages = sorted(
            (
                message
                for message in self.messages.get(conversation_id, {}).values()
                if message.turn_id in selected_ids
            ),
            key=lambda message: (message.created_at, message.id),
        )
        messages = tuple(
            MessageProjection(
                id=message.id,
                turn_id=message.turn_id,
                role=message.role,
                text=message.text,
                sequence=message.sequence,
                interrupted=message.interrupted,
                created_at=message.created_at,
            )
            for message in selected_messages
        )
        tools = tuple(
            ToolProjection(
                id=tool.id,
                turn_id=tool.turn_id,
                tool_name=tool.tool_name,
                arguments=dict(tool.arguments),
                outcome=tool.outcome,
                exit_status=tool.exit_status,
                paths=tool.paths,
                output_tail=tool.output_tail,
            )
            for tool in self.tools.get(conversation_id, {}).values()
            if tool.turn_id in selected_ids
        )
        plans = tuple(
            PlanProjection(id=plan.id, turn_id=plan.turn_id, items=plan.items)
            for plan in self.plans.get(conversation_id, {}).values()
            if plan.turn_id in selected_ids
        )
        selected_activity = sorted(
            (
                item
                for item in self.activities.get(conversation_id, {}).values()
                if item.parent_turn_id in selected_ids
            ),
            key=lambda item: (item.created_at, item.id),
        )
        activity = tuple(
            ActivityProjection(
                id=item.id,
                conversation_id=item.conversation_id,
                parent_turn_id=item.parent_turn_id,
                parent_activity_id=item.parent_activity_id,
                status=item.status,
                title=item.title,
                summary=item.summary,
                created_at=item.created_at,
                completed_at=item.completed_at,
            )
            for item in selected_activity
        )

        pending = [
            self._interaction_proj(i)
            for i in self.interactions.get(conversation_id, {}).values()
            if i.status in {InteractionStatus.PENDING, InteractionStatus.DRAFT}
        ]
        pending.sort(key=lambda i: (i.created_at, i.id))

        active_command = None
        cmd_id = None
        if state.active_turn is not None:
            cmd_id = state.active_turn.command_id
        elif state.queued_turn is not None:
            cmd_id = state.queued_turn.command_id
        if cmd_id is not None:
            cmd = state.commands.get(cmd_id) or self.commands.get(cmd_id)
            if cmd is not None:
                active_command = CommandProjection(
                    id=cmd.id,
                    kind=cmd.kind,
                    status=cmd.status,
                    target_turn_id=cmd.target_turn_id,
                    idempotency_key=cmd.idempotency_key,
                    created_at=cmd.created_at,
                )

        detail = ConversationDetail(
            conversation=state.conversation,
            harness_kind=state.binding.kind if state.binding else None,
            model=state.binding.configuration.model if state.binding else None,
            mode=state.binding.configuration.mode if state.binding else None,
            turns=turn_projs,
            messages=messages,
            tools=tools,
            plans=plans,
            activity=activity,
            pending_interactions=tuple(pending),
            active_command=active_command,
        )
        return ConversationSnapshot(sequence=high_water, detail=detail)

    async def get_high_water_sequence(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        include_deleted: bool = False,
    ) -> int:
        state = self.states.get(conversation_id)
        if (
            state is None
            or state.conversation.owner_id != owner_id
            or (state.conversation.deleted_at is not None and not include_deleted)
        ):
            raise _not_found("conversation")
        return max(0, state.conversation.next_event_sequence - 1)

    async def page_turns(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[TurnProjection]:
        await self._require_owned(conversation_id, owner_id)
        page_size = clamp_page_limit(limit)
        order = self.turn_order.get(conversation_id, [])
        turns_map = self.turns.get(conversation_id, {})
        ordered = [turns_map[tid] for tid in reversed(order) if tid in turns_map]
        # Include any turns not in order index.
        for tid, turn in turns_map.items():
            if tid not in order:
                ordered.append(turn)
        if cursor is not None:
            sort, item_id = decode_cursor(cursor)
            try:
                sort_i = int(sort)
            except ValueError as exc:
                raise DomainError(ErrorCode.INVALID_CURSOR, "invalid cursor") from exc
            # order_index is position+1 ascending; page uses reverse order.
            ordered = [
                t
                for t in ordered
                if (order.index(t.id) + 1 if t.id in order else 0) < sort_i
                or ((order.index(t.id) + 1 if t.id in order else 0) == sort_i and t.id < item_id)
            ]
        page = ordered[:page_size]
        next_cursor = None
        if len(ordered) > page_size and page:
            last = page[-1]
            idx = order.index(last.id) + 1 if last.id in order else 0
            next_cursor = encode_cursor(sort=str(idx), id=last.id)
        return Page(items=tuple(self._turn_proj(t) for t in page), next_cursor=next_cursor)

    async def page_messages(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[MessageProjection]:
        await self._require_owned(conversation_id, owner_id)
        page_size = clamp_page_limit(limit)
        messages = list(self.messages.get(conversation_id, {}).values())
        messages.sort(key=lambda m: (m.created_at, m.id), reverse=True)
        if cursor is not None:
            sort, item_id = decode_cursor(cursor)
            sort_dt = datetime.fromisoformat(sort)
            messages = [
                m
                for m in messages
                if m.created_at < sort_dt or (m.created_at == sort_dt and m.id < item_id)
            ]
        page = messages[:page_size]
        next_cursor = None
        if len(messages) > page_size and page:
            last = page[-1]
            next_cursor = encode_cursor(sort=last.created_at.isoformat(), id=last.id)
        return Page(
            items=tuple(
                MessageProjection(
                    id=m.id,
                    turn_id=m.turn_id,
                    role=m.role,
                    text=m.text,
                    sequence=m.sequence,
                    interrupted=m.interrupted,
                    created_at=m.created_at,
                )
                for m in page
            ),
            next_cursor=next_cursor,
        )

    async def page_tools(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[ToolProjection]:
        await self._require_owned(conversation_id, owner_id)
        page_size = clamp_page_limit(limit)
        tools = list(enumerate(self.tools.get(conversation_id, {}).values(), start=1))
        tools.reverse()
        if cursor is not None:
            sort, item_id = decode_cursor(cursor)
            order_index = int(sort)
            tools = [
                item
                for item in tools
                if item[0] < order_index or (item[0] == order_index and item[1].id < item_id)
            ]
        page = tools[:page_size]
        next_cursor = None
        if len(tools) > page_size and page:
            order_index, last = page[-1]
            next_cursor = encode_cursor(sort=str(order_index), id=last.id)
        return Page(
            items=tuple(
                ToolProjection(
                    id=t.id,
                    turn_id=t.turn_id,
                    tool_name=t.tool_name,
                    arguments=dict(t.arguments),
                    outcome=t.outcome,
                    exit_status=t.exit_status,
                    paths=t.paths,
                    output_tail=t.output_tail,
                )
                for _, t in page
            ),
            next_cursor=next_cursor,
        )

    async def page_plans(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[PlanProjection]:
        await self._require_owned(conversation_id, owner_id)
        page_size = clamp_page_limit(limit)
        plans = list(enumerate(self.plans.get(conversation_id, {}).values(), start=1))
        plans.reverse()
        if cursor is not None:
            sort, item_id = decode_cursor(cursor)
            order_index = int(sort)
            plans = [
                item
                for item in plans
                if item[0] < order_index or (item[0] == order_index and item[1].id < item_id)
            ]
        page = plans[:page_size]
        next_cursor = None
        if len(plans) > page_size and page:
            order_index, last = page[-1]
            next_cursor = encode_cursor(sort=str(order_index), id=last.id)
        return Page(
            items=tuple(
                PlanProjection(id=plan.id, turn_id=plan.turn_id, items=plan.items)
                for _, plan in page
            ),
            next_cursor=next_cursor,
        )

    async def page_activity(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[ActivityProjection]:
        await self._require_owned(conversation_id, owner_id)
        page_size = clamp_page_limit(limit)
        activities = list(self.activities.get(conversation_id, {}).values())
        activities.sort(key=lambda a: (a.created_at, a.id), reverse=True)
        if cursor is not None:
            sort, item_id = decode_cursor(cursor)
            sort_dt = datetime.fromisoformat(sort)
            activities = [
                a
                for a in activities
                if a.created_at < sort_dt or (a.created_at == sort_dt and a.id < item_id)
            ]
        page = activities[:page_size]
        next_cursor = None
        if len(activities) > page_size and page:
            last = page[-1]
            next_cursor = encode_cursor(sort=last.created_at.isoformat(), id=last.id)
        return Page(
            items=tuple(
                ActivityProjection(
                    id=a.id,
                    conversation_id=a.conversation_id,
                    parent_turn_id=a.parent_turn_id,
                    parent_activity_id=a.parent_activity_id,
                    status=a.status,
                    title=a.title,
                    summary=a.summary,
                    created_at=a.created_at,
                    completed_at=a.completed_at,
                )
                for a in page
            ),
            next_cursor=next_cursor,
        )

    async def page_pending_interactions(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[InteractionProjection]:
        await self._require_owned(conversation_id, owner_id)
        page_size = clamp_page_limit(limit)
        items = [
            self._interaction_proj(i)
            for i in self.interactions.get(conversation_id, {}).values()
            if i.status in {InteractionStatus.PENDING, InteractionStatus.DRAFT}
        ]
        items.sort(key=lambda i: (i.created_at, i.id))
        if cursor is not None:
            sort, item_id = decode_cursor(cursor)
            sort_dt = datetime.fromisoformat(sort)
            items = [
                i
                for i in items
                if i.created_at > sort_dt or (i.created_at == sort_dt and i.id > item_id)
            ]
        page = items[:page_size]
        next_cursor = None
        if len(items) > page_size and page:
            last = page[-1]
            next_cursor = encode_cursor(sort=last.created_at.isoformat(), id=last.id)
        return Page(items=tuple(page), next_cursor=next_cursor)

    async def commit_facade_mutation(
        self,
        conversation_id: UUID,
        owner_id: str,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
        commands: Sequence[Command] = (),
        interaction_answers: Sequence[InteractionAnswer] = (),
    ) -> Sequence[ConversationEvent]:
        current = self.states.get(conversation_id)
        if current is None or current.conversation.owner_id != owner_id:
            raise _not_found("conversation")
        # Allow soft-delete mutation when currently not deleted.
        if current.conversation.deleted_at is not None and state.conversation.deleted_at is None:
            raise _not_found("conversation")
        if current.conversation.version != expected_version:
            raise DomainError(
                ErrorCode.OPTIMISTIC_CONFLICT,
                "optimistic concurrency conflict",
                details={
                    "expected": expected_version,
                    "actual": current.conversation.version,
                },
            )
        if state.conversation.owner_id != owner_id:
            raise _not_found("conversation")
        stored = self.events.setdefault(conversation_id, [])
        for event in events:
            stored.append(event)
        self.states[conversation_id] = state
        for command in commands:
            self.commands[command.id] = command
            if command.status == CommandStatus.ACCEPTED and command.id not in self.accepted_queue:
                self.accepted_queue.append(command.id)
        for answer in interaction_answers:
            self.interaction_answers.setdefault(answer.interaction_id, answer)
        self._index_state_projections(state)
        self._refresh_search(state)
        return tuple(events)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _require_owned(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        include_deleted: bool = False,
    ) -> ConversationState:
        state = self.states.get(conversation_id)
        if (
            state is None
            or state.conversation.owner_id != owner_id
            or (state.conversation.deleted_at is not None and not include_deleted)
        ):
            raise _not_found("conversation")
        return state

    def _harness_proj(self, harness: HarnessInstance) -> HarnessProjection:
        return HarnessProjection(
            id=harness.id,
            owner_id=harness.owner_id,
            name=harness.name,
            kind=harness.kind,
            configuration=harness.configuration,
            created_at=harness.created_at,
        )

    def _shell(self, state: ConversationState) -> ConversationShell:
        conv = state.conversation
        binding = state.binding
        pending = any(
            i.status in {InteractionStatus.PENDING, InteractionStatus.DRAFT}
            for i in state.interactions.values()
        )
        return ConversationShell(
            id=conv.id,
            title=conv.display_title,
            status=conv.status,
            harness_kind=binding.kind if binding else None,
            model=binding.configuration.model if binding else None,
            mode=binding.configuration.mode if binding else None,
            has_pending_interactions=pending,
            pinned_at=conv.pinned_at,
            archived_at=conv.archived_at,
            snoozed_until=conv.snoozed_until,
            updated_at=conv.updated_at,
            latest_activity_at=conv.updated_at,
        )

    def _turn_proj(self, turn: Turn) -> TurnProjection:
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

    def _interaction_proj(self, interaction: PendingInteraction) -> InteractionProjection:
        return InteractionProjection(
            id=interaction.id,
            kind=interaction.kind,
            status=interaction.status,
            turn_id=interaction.turn_id,
            request=interaction.request,
            draft=interaction.draft,
            created_at=interaction.created_at,
        )

    def _index_state_projections(self, state: ConversationState) -> None:
        cid = state.conversation.id
        turns = self.turns.setdefault(cid, {})
        order = self.turn_order.setdefault(cid, [])
        for turn in (state.active_turn, state.queued_turn):
            if turn is None:
                continue
            turns[turn.id] = turn
            if turn.id not in order:
                order.append(turn.id)
            # Synthesize a user message from queued text when present.
            if (
                turn.status is TurnStatus.QUEUED
                and state.queued_user_text
                and turn.user_message_id is None
            ):
                from uuid import uuid4

                msg_id = uuid4()
                turn = turn.model_copy(update={"user_message_id": msg_id})
                turns[turn.id] = turn
                self.messages.setdefault(cid, {})[msg_id] = Message(
                    id=msg_id,
                    turn_id=turn.id,
                    role=MessageRole.USER,
                    text=state.queued_user_text,
                    created_at=turn.created_at,
                )
        for interaction in state.interactions.values():
            self.interactions.setdefault(cid, {})[interaction.id] = interaction
        for activity in state.activities.values():
            self.activities.setdefault(cid, {})[activity.id] = activity

    def _apply_events_to_projections(
        self,
        conversation_id: UUID,
        events: Sequence[ConversationEvent],
    ) -> None:
        """Retain terminal turns/messages/tools from the committed event stream.

        ``_index_state_projections`` only sees the live active/queued turns, so a
        single commit that queues, starts, and completes a turn would otherwise
        leave no retained history for handoff/search/retention tests.
        """
        turns = self.turns.setdefault(conversation_id, {})
        order = self.turn_order.setdefault(conversation_id, [])
        messages = self.messages.setdefault(conversation_id, {})
        tools = self.tools.setdefault(conversation_id, {})
        for event in events:
            payload = event.payload
            if isinstance(payload, TurnQueuedPayload):
                turn = turns.get(payload.turn_id) or Turn(
                    id=payload.turn_id,
                    conversation_id=conversation_id,
                    status=TurnStatus.QUEUED,
                    command_id=payload.command_id,
                    created_at=event.timestamp,
                )
                turns[payload.turn_id] = turn
                if payload.turn_id not in order:
                    order.append(payload.turn_id)
                if payload.prompt:
                    from uuid import uuid4

                    msg_id = turn.user_message_id or uuid4()
                    turns[payload.turn_id] = turn.model_copy(update={"user_message_id": msg_id})
                    messages[msg_id] = Message(
                        id=msg_id,
                        turn_id=payload.turn_id,
                        role=MessageRole.USER,
                        text=payload.prompt,
                        created_at=event.timestamp,
                    )
            elif isinstance(payload, TurnStartedPayload):
                existing = turns.get(payload.turn_id)
                if existing is None:
                    turns[payload.turn_id] = Turn(
                        id=payload.turn_id,
                        conversation_id=conversation_id,
                        status=TurnStatus.RUNNING,
                        command_id=payload.command_id,
                        created_at=event.timestamp,
                        started_at=event.timestamp,
                    )
                    if payload.turn_id not in order:
                        order.append(payload.turn_id)
                else:
                    turns[payload.turn_id] = existing.model_copy(
                        update={
                            "status": TurnStatus.RUNNING,
                            "started_at": existing.started_at or event.timestamp,
                            "command_id": payload.command_id or existing.command_id,
                        }
                    )
            elif isinstance(payload, TurnCompletedPayload):
                existing = turns.get(payload.turn_id)
                if existing is not None:
                    turns[payload.turn_id] = existing.model_copy(
                        update={
                            "status": TurnStatus.COMPLETED,
                            "completed_at": event.timestamp,
                            "terminal_reason": payload.terminal_reason,
                        }
                    )
            elif isinstance(payload, TurnInterruptedPayload):
                existing = turns.get(payload.turn_id)
                if existing is not None:
                    turns[payload.turn_id] = existing.model_copy(
                        update={
                            "status": TurnStatus.INTERRUPTED,
                            "completed_at": event.timestamp,
                            "terminal_reason": payload.reason,
                        }
                    )
            elif isinstance(payload, TurnFailedPayload):
                existing = turns.get(payload.turn_id)
                if existing is not None:
                    turns[payload.turn_id] = existing.model_copy(
                        update={
                            "status": TurnStatus.FAILED,
                            "completed_at": event.timestamp,
                            "terminal_reason": payload.message,
                        }
                    )
            elif isinstance(payload, TurnOutcomeUnknownPayload):
                existing = turns.get(payload.turn_id)
                if existing is not None:
                    turns[payload.turn_id] = existing.model_copy(
                        update={
                            "status": TurnStatus.OUTCOME_UNKNOWN,
                            "completed_at": event.timestamp,
                            "terminal_reason": payload.message,
                        }
                    )
            elif isinstance(payload, TurnCancelledPayload):
                existing = turns.get(payload.turn_id)
                if existing is not None:
                    turns[payload.turn_id] = existing.model_copy(
                        update={
                            "status": TurnStatus.INTERRUPTED,
                            "completed_at": event.timestamp,
                            "terminal_reason": "cancelled",
                        }
                    )
            elif isinstance(payload, AssistantMessageStartedPayload):
                messages[payload.message_id] = Message(
                    id=payload.message_id,
                    turn_id=payload.turn_id,
                    role=MessageRole.ASSISTANT,
                    text="",
                    created_at=event.timestamp,
                )
            elif isinstance(payload, AssistantMessageCompletedPayload):
                existing = messages.get(payload.message_id)
                if existing is not None:
                    messages[payload.message_id] = existing.model_copy(
                        update={"text": payload.text, "completed": True}
                    )
                else:
                    messages[payload.message_id] = Message(
                        id=payload.message_id,
                        turn_id=payload.turn_id,
                        role=MessageRole.ASSISTANT,
                        text=payload.text,
                        completed=True,
                        created_at=event.timestamp,
                    )
            elif isinstance(payload, ToolRequestedPayload):
                tools[payload.tool_id] = CanonicalToolResult(
                    id=payload.tool_id,
                    turn_id=payload.turn_id,
                    tool_name=payload.tool_name,
                    arguments=dict(payload.arguments),
                    outcome=ToolOutcome.UNKNOWN,
                )
            elif isinstance(payload, ToolCompletedPayload):
                existing = tools.get(payload.tool_id)
                base = existing or CanonicalToolResult(
                    id=payload.tool_id,
                    turn_id=payload.turn_id,
                    tool_name=payload.tool_name,
                )
                tools[payload.tool_id] = base.model_copy(
                    update={
                        "tool_name": payload.tool_name,
                        "outcome": payload.outcome,
                        "exit_status": payload.exit_status,
                        "output_tail": payload.output_tail,
                    }
                )

    def _refresh_search(self, state: ConversationState) -> None:
        cid = state.conversation.id
        messages = self.messages.get(cid, {}).values()
        tools = self.tools.get(cid, {}).values()
        self.search_docs[cid] = build_search_document_from_parts(
            title=state.conversation.display_title,
            message_texts=[m.text for m in messages],
            tool_names=[t.tool_name for t in tools],
            tool_arguments=[dict(t.arguments) for t in tools],
            tool_paths=[p for t in tools for p in t.paths],
            tool_output_tails=[t.output_tail for t in tools if t.output_tail],
        )
