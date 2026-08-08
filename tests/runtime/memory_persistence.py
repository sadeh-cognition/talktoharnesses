"""In-memory Persistence for runtime lifecycle and facade contract tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast
from uuid import UUID

from talktoharnesses.application.cursors import clamp_page_limit, decode_cursor, encode_cursor
from talktoharnesses.application.search_documents import build_search_document_from_parts
from talktoharnesses.domain.approval_matching import (
    InteractionMatchContext,
    select_matching_rule,
)
from talktoharnesses.domain.enums import (
    ApprovalDecision,
    ApprovalRuleDecision,
    CommandStatus,
    ErrorCode,
    InteractionKind,
    InteractionStatus,
    TurnStatus,
)
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import ConversationEvent
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
    ToolProjection,
    Turn,
    TurnProjection,
)
from talktoharnesses.domain.transitions import ConversationState, submit_interaction_answer


def _not_found(resource: str = "conversation") -> DomainError:
    return DomainError(ErrorCode.NOT_FOUND, f"{resource} not found")


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
        self.search_docs: dict[UUID, str] = {}
        self.turn_order: dict[UUID, list[UUID]] = {}

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

    async def claim_commands(self, worker_id: str, limit: int) -> Sequence[Command]:
        from datetime import UTC, datetime, timedelta

        claimed: list[Command] = []
        still_pending: list[UUID] = []
        now = datetime.now(UTC)
        lease = now + timedelta(seconds=30)
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
            updated = command.model_copy(
                update={
                    "status": CommandStatus.CLAIMED,
                    "worker_id": worker_id,
                    "attempts": command.attempts + 1,
                    "lease_expires_at": lease,
                }
            )
            self.commands[command_id] = updated
            claimed.append(updated)
        self.accepted_queue = still_pending
        return tuple(claimed)

    async def renew_command_lease(
        self,
        command_id: UUID,
        worker_id: str,
        expires_at: datetime,
    ) -> None:
        command = self.commands.get(command_id)
        if command is None or command.worker_id != worker_id:
            raise DomainError(ErrorCode.INVALID_STATE, "command lease not found for worker")
        self.commands[command_id] = command.model_copy(update={"lease_expires_at": expires_at})

    async def update_command(self, command: Command) -> Command:
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
    ) -> Sequence[ConversationEvent]:
        return await self.commit_turn_batch(
            conversation_id,
            expected_version,
            state,
            events,
            (),
        )

    async def commit_turn_batch(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
        commands: Sequence[Command] = (),
    ) -> Sequence[ConversationEvent]:
        committed = await self.commit_runtime_lifecycle(
            conversation_id,
            expected_version,
            state,
            None,
            None,
            events,
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
    ) -> Sequence[ConversationEvent]:
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
    ) -> Sequence[ConversationEvent]:
        committed = await self.commit_turn_batch(conversation_id, expected_version, state, events)
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
    ) -> InteractionResolutionResult:
        from uuid import uuid4

        iid = interaction_id or answer.interaction_id
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
            await self.commit_turn_batch(conversation_id, expected_version, state, events)
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
    ) -> Command:
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

    async def delete_expired_turn_aggregates(self, cutoff: datetime) -> int:
        return 0

    async def purge_soft_deleted(self, cutoff: datetime) -> int:
        to_delete = [
            cid
            for cid, state in self.states.items()
            if state.conversation.deleted_at is not None and state.conversation.deleted_at < cutoff
        ]
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
    ) -> Page[ConversationShell]:
        page_size = clamp_page_limit(limit)
        needle = " ".join(query.split()).casefold()
        if not needle:
            return Page(items=(), next_cursor=None)
        matches: list[ConversationShell] = []
        for cid, doc in self.search_docs.items():
            state = self.states.get(cid)
            if state is None or state.conversation.owner_id != owner_id:
                continue
            if state.conversation.deleted_at is not None:
                continue
            if needle in doc:
                matches.append(self._shell(state))
        matches.sort(key=lambda s: (s.updated_at, s.id), reverse=True)
        if cursor is not None:
            sort, item_id = decode_cursor(cursor)
            sort_dt = datetime.fromisoformat(sort)
            matches = [
                s
                for s in matches
                if s.updated_at < sort_dt or (s.updated_at == sort_dt and s.id < item_id)
            ]
        page = matches[:page_size]
        next_cursor = None
        if len(matches) > page_size and page:
            last = page[-1]
            next_cursor = encode_cursor(sort=last.updated_at.isoformat(), id=last.id)
        return Page(items=tuple(page), next_cursor=next_cursor)

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

                from talktoharnesses.domain.enums import MessageRole

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
