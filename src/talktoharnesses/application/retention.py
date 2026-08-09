"""Owner-scoped retention cutoff, eligibility, and cleanup orchestration.

``months_before`` is the one pure calendar calculation used by policy preview
and cleanup. ``classify_history_eligibility`` is shared by read-only preview
and mutating prune so both agree on exempt / running / waiting rules.
``run_cleanup`` is the async orchestration the ``talktoharnesses_cleanup``
management command calls; every transaction it drives lives in ``Persistence``
so the same pass runs against Django or the in-memory store.
"""

from __future__ import annotations

import calendar
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from talktoharnesses.application.handoff import render_handoff
from talktoharnesses.application.observability import get_observability
from talktoharnesses.application.persistence import Persistence, PruneResult
from talktoharnesses.application.publisher import CommittedEventPublisher
from talktoharnesses.domain._base import require_utc
from talktoharnesses.domain.enums import ActivityStatus, TurnStatus
from talktoharnesses.domain.models import Turn
from talktoharnesses.domain.transitions import ConversationState
from talktoharnesses.runtime.manager import RuntimeManager

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_MONTHS = 6

_TERMINAL_TURN_STATUSES = frozenset(
    {
        TurnStatus.COMPLETED,
        TurnStatus.INTERRUPTED,
        TurnStatus.FAILED,
        TurnStatus.OUTCOME_UNKNOWN,
    }
)


def months_before(now: datetime, months: int) -> datetime:
    """Return ``now`` shifted back ``months`` calendar months.

    Preserves the time-of-day and UTC offset; the day is clamped to the last
    day of the target month (e.g. Aug 31 -> Feb 28/29). No ``dateutil``.
    """
    ts = require_utc(now)
    total_months = ts.year * 12 + (ts.month - 1) - months
    year, month0 = divmod(total_months, 12)
    month = month0 + 1
    last_day = calendar.monthrange(year, month)[1]
    day = min(ts.day, last_day)
    return ts.replace(year=year, month=month, day=day)


def six_months_before(now: datetime) -> datetime:
    """Return ``now`` shifted back six calendar months (default policy)."""
    return months_before(now, DEFAULT_RETENTION_MONTHS)


def soft_delete_purge_eligible(deleted_at: datetime | None, cutoff: datetime) -> bool:
    """True when a soft-deleted conversation is past the owner's cutoff."""
    return deleted_at is not None and deleted_at <= cutoff


def is_terminal_turn_expired(
    status: TurnStatus,
    completed_at: datetime | None,
    cutoff: datetime,
) -> bool:
    """True when a terminal turn's ``completed_at`` is at or before ``cutoff``."""
    return status in _TERMINAL_TURN_STATUSES and completed_at is not None and completed_at <= cutoff


def is_waiting_turn_expired(active: Turn | None, cutoff: datetime) -> bool:
    """True when the active turn is ``WAITING`` and started/created at or before cutoff."""
    return (
        active is not None
        and active.status is TurnStatus.WAITING
        and (active.started_at or active.created_at) <= cutoff
    )


@dataclass(frozen=True, slots=True)
class HistoryEligibility:
    """Shared history-prune decision for one conversation at one cutoff.

    ``blocked`` means preview/cleanup skip the conversation entirely (exempt,
    no binding, background-active, or a non-expired active turn). When not
    blocked, ``waiting_expired`` marks an active WAITING turn that cleanup
    cancels before pruning.
    """

    blocked: bool
    waiting_expired: bool = False


def classify_history_eligibility(
    state: ConversationState,
    cutoff: datetime,
) -> HistoryEligibility:
    """Classify whether history prune may run for ``state`` at ``cutoff``.

    Exemption skips history prune/rotation. Soft-delete purge is separate and
    ignores exemption. Running turns and running background activities skip
    exactly as Phase 8 cleanup does.
    """
    if state.conversation.retention_exempt:
        return HistoryEligibility(blocked=True)
    if state.binding is None:
        return HistoryEligibility(blocked=True)
    if any(activity.status is ActivityStatus.RUNNING for activity in state.activities.values()):
        return HistoryEligibility(blocked=True)
    waiting_expired = is_waiting_turn_expired(state.active_turn, cutoff)
    if state.active_turn is not None and not waiting_expired:
        return HistoryEligibility(blocked=True)
    return HistoryEligibility(blocked=False, waiting_expired=waiting_expired)


@dataclass(frozen=True, slots=True)
class CleanupCounts:
    """Counts printed by the ``talktoharnesses_cleanup`` management command."""

    purged_conversations: int = 0
    pruned_turns: int = 0
    cancelled_waiting_turns: int = 0
    successful_rotations: int = 0
    bindings_requiring_recreation: int = 0


@dataclass(frozen=True, slots=True)
class DryRunCounts:
    """Aggregate read-only preview fields across owners for ``--dry-run``."""

    soft_deleted_conversations: int = 0
    history_conversations: int = 0
    terminal_turns: int = 0
    waiting_turns: int = 0


async def preview_cleanup(
    persistence: Persistence,
    clock: Callable[[], datetime],
) -> DryRunCounts:
    """Aggregate ``preview_retention`` across every owner with conversations."""
    now = clock()
    soft_deleted = 0
    history = 0
    terminal = 0
    waiting = 0
    for owner_id in await persistence.list_retention_owner_ids():
        preview = await persistence.preview_retention(owner_id, now=now)
        soft_deleted += preview.soft_deleted_conversations
        history += preview.history_conversations
        terminal += preview.terminal_turns
        waiting += preview.waiting_turns
    return DryRunCounts(
        soft_deleted_conversations=soft_deleted,
        history_conversations=history,
        terminal_turns=terminal,
        waiting_turns=waiting,
    )


async def run_cleanup(
    persistence: Persistence,
    runtime_manager: RuntimeManager,
    clock: Callable[[], datetime],
    publisher: CommittedEventPublisher | None = None,
) -> CleanupCounts:
    """Run one retention pass and return counts for the caller to print.

    Captures one UTC ``now``, resolves each owner's effective month count, then
    processes short per-conversation transactions: soft-delete purge,
    terminal-turn pruning, waiting-turn cancellation, and candidate-runtime
    session rotation. Pruning already committed the ``session_rotated`` event
    and cleared the native session ID, so a failed replacement session only
    marks the binding for recreation.
    """
    now = clock()
    purged = await persistence.purge_soft_deleted(now)

    pruned_turns = 0
    cancelled_waiting = 0
    rotated = 0
    requires_recreation = 0
    policy_months: dict[str, int] = {}
    for conversation_id, owner_id in await persistence.list_cleanup_conversation_ids():
        months = policy_months.get(owner_id)
        if months is None:
            months = (await persistence.get_retention_policy(owner_id)).months
            policy_months[owner_id] = months
        cutoff = months_before(now, months)
        result = await persistence.prune_expired_history(conversation_id, cutoff)
        if result is None:
            continue
        pruned_turns += result.pruned_turn_count
        cancelled_waiting += result.cancelled_waiting_count
        get_observability().observe_committed_events(result.session_rotated_events)
        if publisher is not None and result.session_rotated_events:
            await publisher.publish(result.session_rotated_events)
        if not result.handoff.entries:
            # Nothing to carry over: the cleared binding creates lazily.
            continue
        if await _rotate_native_session(persistence, runtime_manager, result):
            rotated += 1
        else:
            requires_recreation += 1

    return CleanupCounts(
        purged_conversations=purged,
        pruned_turns=pruned_turns,
        cancelled_waiting_turns=cancelled_waiting,
        successful_rotations=rotated,
        bindings_requiring_recreation=requires_recreation,
    )


async def _rotate_native_session(
    persistence: Persistence,
    runtime_manager: RuntimeManager,
    result: PruneResult,
) -> bool:
    """Seed a replacement native session for the unchanged active binding."""
    try:
        candidate = await runtime_manager.start_candidate(
            conversation_id=result.conversation_id,
            owner_id=result.owner_id,
            binding_id=result.binding_id,
            configuration=result.configuration,
        )
    except Exception:
        logger.warning(
            "retention rotation could not start a candidate conversation=%s",
            result.conversation_id,
            exc_info=True,
        )
        await persistence.commit_rotation_requires_recreation(
            result.conversation_id, result.version
        )
        return False
    try:
        await runtime_manager.seed_candidate(candidate, render_handoff(result.handoff))
        await persistence.commit_session_rotation(
            result.conversation_id,
            result.version,
            native_session_id=candidate.session.native_session_id,
            launch_snapshot=candidate.launch,
        )
    except Exception:
        logger.warning(
            "retention rotation rejected the candidate conversation=%s",
            result.conversation_id,
            exc_info=True,
        )
        await persistence.commit_rotation_requires_recreation(
            result.conversation_id, result.version
        )
        return False
    finally:
        # The replacement session is durable; only the transient runtime closes.
        await runtime_manager.close_candidate(result.binding_id)
    return True
