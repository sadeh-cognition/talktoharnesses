"""Coarse asynchronous persistence protocols (business operations, not CRUD)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from talktoharnesses.domain.events import ConversationEvent
from talktoharnesses.domain.models import (
    ActivityProjection,
    ApprovalRule,
    ApprovalRuleProjection,
    Command,
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
    MessageProjection,
    Page,
    PlanProjection,
    ProcessRecord,
    ToolProjection,
    TurnProjection,
)
from talktoharnesses.domain.transitions import ConversationState


class Persistence(Protocol):
    """Injected durable state boundary required for all execution paths.

    Implementations (e.g. Django) live in later phases. Callers must refuse
    to execute turns when no persistence implementation is configured
    (``ErrorCode.PERSISTENCE_REQUIRED``).
    """

    async def get_snapshot(self, conversation_id: UUID, owner_id: str) -> ConversationState:
        """Load an owner-scoped conversation aggregate snapshot."""
        ...

    async def get_worker_snapshot(self, conversation_id: UUID) -> ConversationState:
        """Load a conversation aggregate for a worker (no owner check)."""
        ...

    async def save_snapshot(self, state: ConversationState) -> ConversationState:
        """Persist a snapshot using optimistic concurrency on ``version``."""
        ...

    async def accept_command(self, command: Command) -> Command:
        """Accept a command idempotently by ``(conversation_id, idempotency_key)``."""
        ...

    async def claim_commands(self, worker_id: str, limit: int) -> Sequence[Command]:
        """Claim accepted work for a worker under lease semantics."""
        ...

    async def renew_command_lease(
        self,
        command_id: UUID,
        worker_id: str,
        expires_at: datetime,
    ) -> None:
        """Extend a claimed command lease owned by ``worker_id``."""
        ...

    async def update_command(self, command: Command) -> Command:
        """Update command delivery/settlement fields."""
        ...

    async def commit_event_batch(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
    ) -> Sequence[ConversationEvent]:
        """Atomically persist projection state and conversation events."""
        ...

    async def commit_turn_batch(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
        commands: Sequence[Command] = (),
    ) -> Sequence[ConversationEvent]:
        """Atomically persist aggregate, events, and command rows (no process)."""
        ...

    async def commit_runtime_lifecycle(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        process: ProcessRecord | None,
        launch_history_entry: LaunchSnapshot | None,
        events: Sequence[ConversationEvent],
    ) -> Sequence[ConversationEvent]:
        """Atomically update aggregate, process record/tail, launch history, events.

        Retries are idempotent by process-incarnation UUID on ``process.id``.
        Sequence allocation uses the same conversation-local scheme as
        ``commit_event_batch``.
        """
        ...

    async def replay(
        self,
        conversation_id: UUID,
        after_sequence: int,
        event_count_limit: int,
        byte_limit: int,
    ) -> Sequence[ConversationEvent]:
        """Replay committed events after ``after_sequence`` within limits."""
        ...

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
        """Persist request transition, private correlation, and request event sequence."""
        ...

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
        """Atomic first-write-wins resolution with optional rule create and audit.

        Does not create the answer_interaction command (publication-gated release).
        When no matching rule exists for automatic evaluation, callers may pass
        empty events and ``mark_policy_evaluated=True`` to stamp evaluation only.
        """
        ...

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
        """Create or return the unique answer_interaction command after publication."""
        ...

    async def get_interaction_resolution_event(
        self,
        conversation_id: UUID,
        interaction_id: UUID,
    ) -> ConversationEvent:
        """Load the exact committed resolution event recorded for an answer."""
        ...

    async def get_interaction_request_event(
        self,
        conversation_id: UUID,
        interaction_id: UUID,
    ) -> ConversationEvent:
        """Load the exact committed request event recorded for an interaction."""
        ...

    async def complete_suppressed_interaction_resolution(
        self,
        interaction_id: UUID,
        published_at: datetime,
    ) -> bool:
        """Mark a published command-suppressed resolution; return whether suppressed."""
        ...

    async def mark_interaction_policy_evaluated(
        self,
        interaction_id: UUID,
        evaluated_at: datetime,
    ) -> None:
        """Stamp completed no-match policy evaluation (no resolution)."""
        ...

    async def list_unevaluated_open_interactions(self) -> Sequence[tuple[UUID, UUID]]:
        """Return (conversation_id, interaction_id) for open unevaluated requests."""
        ...

    async def list_unreleased_resolutions(self) -> Sequence[tuple[UUID, UUID]]:
        """Return (conversation_id, interaction_id) for resolved but unreleased answers."""
        ...

    async def create_approval_rule(self, rule: ApprovalRule) -> ApprovalRuleProjection:
        """Persist a new live approval rule for its principal."""
        ...

    async def get_approval_rule(self, rule_id: UUID, principal_id: str) -> ApprovalRuleProjection:
        """Owner/principal-scoped rule get; cross-owner ≡ missing."""
        ...

    async def replace_approval_rule(self, rule: ApprovalRule) -> ApprovalRuleProjection:
        """Replace decision/scope/matcher for an owned live rule."""
        ...

    async def delete_approval_rule(self, rule_id: UUID, principal_id: str) -> None:
        """Hard-delete only the live rule row."""
        ...

    async def page_approval_rules(
        self,
        principal_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[ApprovalRuleProjection]:
        """Keyset-page live rules (created_at DESC, id DESC)."""
        ...

    async def list_applicable_approval_rules(self, principal_id: str) -> Sequence[ApprovalRule]:
        """Load all live rules for a principal (broker evaluation)."""
        ...

    async def get_interaction_audit(
        self, audit_id: UUID, principal_id: str
    ) -> InteractionAuditProjection:
        """Owner-scoped immutable audit get."""
        ...

    async def page_interaction_audits(
        self,
        principal_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[InteractionAuditProjection]:
        """Keyset-page audits (created_at DESC, id DESC)."""
        ...

    async def delete_expired_turn_aggregates(self, cutoff: datetime) -> int:
        """Retention: delete complete expired turn aggregates. Returns count."""
        ...

    async def purge_soft_deleted(self, cutoff: datetime) -> int:
        """Retention: permanently purge soft-deleted conversations. Returns count."""
        ...

    # ------------------------------------------------------------------
    # Phase 5 facade projection surface
    # ------------------------------------------------------------------

    async def create_harness(self, harness: HarnessInstance) -> HarnessProjection:
        """Persist a new owner-scoped harness configuration."""
        ...

    async def list_harnesses(
        self,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[HarnessProjection]:
        """Keyset-page harnesses for one owner (created_at DESC, id DESC)."""
        ...

    async def get_harness(self, harness_id: UUID, owner_id: str) -> HarnessProjection:
        """Load one owner-scoped harness; cross-owner ≡ missing."""
        ...

    async def save_harness_probe(
        self,
        harness_id: UUID,
        owner_id: str,
        capabilities: HarnessCapabilities,
        *,
        probed_at: datetime,
    ) -> HarnessProbeProjection:
        """Persist the last successful probe for an owned harness."""
        ...

    async def get_harness_probe(
        self,
        harness_id: UUID,
        owner_id: str,
    ) -> HarnessProbeProjection:
        """Return the last successful probe projection for an owned harness."""
        ...

    async def list_conversations(
        self,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
        include_archived: bool = True,
    ) -> Page[ConversationShell]:
        """Keyset-page conversation shells (updated_at DESC, id DESC). Soft-deleted excluded."""
        ...

    async def search_conversations(
        self,
        owner_id: str,
        query: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[ConversationShell]:
        """Portable case-insensitive substring search over sanitized documents."""
        ...

    async def get_conversation_snapshot(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        include_deleted: bool = False,
    ) -> ConversationSnapshot:
        """Sequence-stamped detail (20 user-anchored turns) in one transaction."""
        ...

    async def get_high_water_sequence(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        include_deleted: bool = False,
    ) -> int:
        """Committed conversation-local high-water sequence (owner-scoped)."""
        ...

    async def page_turns(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[TurnProjection]: ...

    async def page_messages(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[MessageProjection]: ...

    async def page_tools(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[ToolProjection]: ...

    async def page_plans(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[PlanProjection]: ...

    async def page_activity(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[ActivityProjection]: ...

    async def page_pending_interactions(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[InteractionProjection]:
        """Pending/draft interactions ordered created_at ASC, id ASC."""
        ...

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
        """Atomic aggregate, projections, events, commands, and answers commit.

        Returns only the committed events for broker publication.
        """
        ...
