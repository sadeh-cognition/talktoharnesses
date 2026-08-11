"""Coarse asynchronous persistence protocols (business operations, not CRUD)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from talktoharnesses.application.handoff import HandoffDocument
from talktoharnesses.domain.events import ConversationEvent
from talktoharnesses.domain.models import (
    ActivityProjection,
    ApprovalRule,
    ApprovalRuleProjection,
    Command,
    ConversationSearchHit,
    ConversationShell,
    ConversationSnapshot,
    HarnessCapabilities,
    HarnessConfiguration,
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
    RetentionPolicyProjection,
    RetentionPreviewProjection,
    ToolProjection,
    TurnProjection,
)
from talktoharnesses.domain.transitions import ConversationState


@dataclass(frozen=True, slots=True)
class PruneResult:
    """Outcome of one conversation's history-pruning transaction.

    Carries everything ``run_cleanup`` needs to attempt candidate-runtime
    session rotation without a second read: the previous native session
    identity, the active binding's configuration, the retained handoff to
    seed a replacement session, and the state version/events already
    committed by the prune itself.
    """

    conversation_id: UUID
    owner_id: str
    binding_id: UUID
    previous_native_session_id: str | None
    configuration: HarnessConfiguration
    handoff: HandoffDocument
    version: int
    session_rotated_events: tuple[ConversationEvent, ...] = ()
    pruned_turn_count: int = 0
    cancelled_waiting_count: int = 0


@dataclass(frozen=True, slots=True)
class SwitchPreparation:
    """Aggregate and retained handoff read from one committed version."""

    state: ConversationState
    handoff: HandoffDocument


@dataclass(frozen=True, slots=True)
class ClaimedCommand:
    command: Command
    fence: int


@dataclass(frozen=True, slots=True)
class ConversationOwnership:
    conversation_id: UUID
    worker_id: str
    fence: int
    lease_expires_at: datetime
    recovery_attempt_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RecoveryAttempt:
    id: UUID
    conversation_id: UUID
    binding_id: UUID
    command_id: UUID | None
    turn_id: UUID | None
    worker_id: str
    fence: int
    trigger: str
    observed_delivery_phase: str
    action: str
    result: str | None
    reason_code: str
    started_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class LostLease:
    conversation_id: UUID
    fence: int


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

    async def claim_commands(
        self,
        worker_id: str,
        limit: int,
        *,
        lease_duration: float,
    ) -> Sequence[ClaimedCommand]:
        """Claim accepted work after acquiring/renewing conversation ownership."""
        ...

    async def renew_command_lease(
        self,
        command_id: UUID,
        worker_id: str,
        *,
        lease_duration: float,
        fence: int | None = None,
    ) -> None:
        """Extend a claimed/in-flight command lease owned by ``worker_id``."""
        ...

    async def update_command(
        self,
        command: Command,
        *,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> Command:
        """Update command delivery/settlement fields."""
        ...

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
        """Atomically persist projection state and conversation events."""
        ...

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
        *,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> Sequence[ConversationEvent]:
        """Atomically update aggregate, process record/tail, launch history, events.

        Retries are idempotent by process-incarnation UUID on ``process.id``.
        Sequence allocation uses the same conversation-local scheme as
        ``commit_event_batch``.
        """
        ...

    async def acquire_worker_lease(
        self,
        worker_id: str,
        *,
        lease_duration: float,
        slot: str | None = None,
    ) -> None:
        """Acquire the process worker lease (SQLite singleton or per-worker)."""
        ...

    async def renew_worker_lease(self, worker_id: str, *, lease_duration: float) -> None:
        """Heartbeat-renew the process worker lease owned by ``worker_id``."""
        ...

    async def mark_worker_draining(self, worker_id: str) -> None:
        """Mark the process worker lease as draining."""
        ...

    async def release_worker_lease(self, worker_id: str) -> None:
        """Release the process worker lease owned by ``worker_id``."""
        ...

    async def claim_expired_conversations(
        self,
        worker_id: str,
        limit: int,
        *,
        lease_duration: float,
        trigger: str = "takeover",
    ) -> Sequence[ConversationOwnership]:
        """Take over expired active conversations and start recovery attempts."""
        ...

    async def renew_owned_conversation_leases(
        self,
        worker_id: str,
        *,
        lease_duration: float,
    ) -> Sequence[LostLease]:
        """Renew conversation leases owned by ``worker_id``; return lost fences."""
        ...

    async def release_conversation_lease(
        self,
        conversation_id: UUID,
        worker_id: str,
        fence: int,
    ) -> None:
        """Release an idle conversation lease when ownership still matches."""
        ...

    async def complete_recovery_attempt(
        self,
        attempt_id: UUID,
        *,
        result: str,
        reason_code: str,
        completed_at: datetime,
    ) -> None:
        """Mark an in-progress recovery attempt terminal with fixed codes."""
        ...

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
        """Atomically persist fenced recovery state, suppression, and attempt metadata."""
        ...

    async def get_open_recovery_attempt(
        self,
        conversation_id: UUID,
        worker_id: str,
        fence: int,
    ) -> RecoveryAttempt | None:
        """Return the unfinished recovery attempt for a fenced owner, if any."""
        ...

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
        """Persist the classifier decision while fenced ownership is current."""
        ...

    async def mark_incomplete_assistant_messages_interrupted(
        self,
        conversation_id: UUID,
        turn_id: UUID,
    ) -> None:
        """Mark incomplete assistant messages for ``turn_id`` as interrupted."""
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
        worker_id: str | None = None,
        fence: int | None = None,
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
        worker_id: str | None = None,
        fence: int | None = None,
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
        worker_id: str | None = None,
        fence: int | None = None,
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

    async def read_retained_handoff(
        self,
        conversation_id: UUID,
        *,
        owner_id: str | None = None,
    ) -> HandoffDocument:
        """Read the ordered retained canonical handoff for one conversation.

        Owner-scoped (cross-owner ≡ missing) when ``owner_id`` is given;
        worker-scoped when omitted, for switch/rotation use from the command
        processor. Merges rows by ``(turn.order_index, item.order_index,
        id)``. Reasoning, plans, raw events, and full tool output are never
        read here — see ``application.handoff``.
        """
        ...

    async def read_retained_export(
        self,
        conversation_id: UUID,
        owner_id: str,
    ) -> tuple[HandoffDocument, str]:
        """Read retained handoff and effective title from one committed version."""
        ...

    async def commit_transcript_import(
        self,
        state: ConversationState,
        handoff: HandoffDocument,
        events: Sequence[ConversationEvent],
        *,
        process: ProcessRecord | None = None,
        launch_history_entry: LaunchSnapshot | None = None,
    ) -> Sequence[ConversationEvent]:
        """Atomically create an imported conversation and its retained history.

        Creates the aggregate, active binding, canonical turn/message/tool rows
        from ``handoff``, search document, launch/session state, and the
        bounded ``transcript_imported`` event. Never calls a provider.
        """
        ...

    async def prepare_harness_switch(self, conversation_id: UUID) -> SwitchPreparation:
        """Lock and read the switch aggregate and handoff in one transaction."""
        ...

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
        """Atomically commit a successful harness switch.

        Closes the previous binding row, inserts the new active binding from
        ``state.binding``, updates aggregate/shell/search projections,
        persists the accepted candidate process/launch record, settles
        ``command``, and inserts ``harness_switched``. The caller closes the
        replaced runtime/session only after this commit succeeds.
        """
        ...

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
        """Atomically commit a failed harness switch against the unchanged binding.

        Settles ``command`` with its sanitized error/message and inserts only
        ``harness_switch_failed``; the current binding and runtime are never
        touched by a failed switch.
        """
        ...

    async def get_retention_policy(self, owner_id: str) -> RetentionPolicyProjection:
        """Return the owner's effective retention policy (absent => months=6)."""
        ...

    async def replace_retention_policy(
        self,
        owner_id: str,
        months: int,
        *,
        now: datetime,
    ) -> RetentionPolicyProjection:
        """Upsert the owner's retention month count."""
        ...

    async def preview_retention(
        self,
        owner_id: str,
        *,
        now: datetime,
    ) -> RetentionPreviewProjection:
        """Read-only eligible retention counts for one owner at ``now``."""
        ...

    async def list_retention_owner_ids(self) -> Sequence[str]:
        """Return distinct owner IDs that have any conversation (live or deleted)."""
        ...

    async def list_cleanup_conversation_ids(self) -> Sequence[tuple[UUID, str]]:
        """Return ``(conversation_id, owner_id)`` for every non-soft-deleted conversation."""
        ...

    async def prune_expired_history(
        self,
        conversation_id: UUID,
        cutoff: datetime,
    ) -> PruneResult | None:
        """Delete one conversation's terminal-expired turn history in one transaction.

        Returns ``None`` when the conversation is retention-exempt, a turn or
        background activity is running, or nothing is eligible. An expired
        ``WAITING`` turn's open interactions are cancelled and settled before
        that turn is pruned. Recomputes the derived title, shell fields, and
        search document, and returns the retained handoff plus the previous
        native session identity needed for rotation.
        """
        ...

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
        """Commit successful post-prune rotation.

        Records the new native session ID and launch snapshot on the active
        binding and clears ``requires_session_recreation``.
        """
        ...

    async def commit_rotation_requires_recreation(
        self,
        conversation_id: UUID,
        expected_version: int,
        *,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> None:
        """Mark the active binding as requiring recreation after failed rotation.

        History deletion already succeeded; only replacement-session creation
        failed. The next command must create a fresh session.
        """
        ...

    async def purge_soft_deleted(self, now: datetime) -> int:
        """Permanently purge soft-deleted conversations using per-owner cutoffs.

        Each owner's effective policy months determine its cutoff from ``now``.
        Matches ``deleted_at <= cutoff`` so a boundary row is purged on the run
        whose cutoff equals it; exemption does not prevent soft-delete purge.
        """
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

    async def delete_harness(self, harness_id: UUID, owner_id: str) -> None:
        """Delete an idle owner-scoped harness while retaining copied binding history."""
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

    async def list_configured_harnesses_for_readiness(self) -> Sequence[HarnessProjection]:
        """All configured harnesses ordered by harness_id (no owner filter)."""
        ...

    async def has_fresh_harness_probe(
        self,
        *,
        now: datetime,
        max_age_seconds: int = 300,
    ) -> bool:
        """True when any harness has a successful probe within the freshness window."""
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
    ) -> Page[ConversationSearchHit]:
        """Ranked search over sanitized documents with bounded snippets."""
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
