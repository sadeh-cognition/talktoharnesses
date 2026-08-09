"""Six-month retention cutoff and cleanup orchestration entry point.

``six_months_before`` is the one pure calendar calculation used by both the
cutoff and eligibility checks in ``docs/phase8.md`` Work Package 5.
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
from talktoharnesses.application.persistence import Persistence, PruneResult
from talktoharnesses.application.publisher import CommittedEventPublisher
from talktoharnesses.domain._base import require_utc
from talktoharnesses.runtime.manager import RuntimeManager

logger = logging.getLogger(__name__)

_MONTHS_RETAINED = 6


def six_months_before(now: datetime) -> datetime:
    """Return ``now`` shifted back six calendar months.

    Preserves the time-of-day and UTC offset; the day is clamped to the last
    day of the target month (e.g. Aug 31 -> Feb 28/29). No ``dateutil``.
    """
    ts = require_utc(now)
    total_months = ts.year * 12 + (ts.month - 1) - _MONTHS_RETAINED
    year, month0 = divmod(total_months, 12)
    month = month0 + 1
    last_day = calendar.monthrange(year, month)[1]
    day = min(ts.day, last_day)
    return ts.replace(year=year, month=month, day=day)


@dataclass(frozen=True, slots=True)
class CleanupCounts:
    """Counts printed by the ``talktoharnesses_cleanup`` management command."""

    purged_conversations: int = 0
    pruned_turns: int = 0
    cancelled_waiting_turns: int = 0
    successful_rotations: int = 0
    bindings_requiring_recreation: int = 0


async def run_cleanup(
    persistence: Persistence,
    runtime_manager: RuntimeManager,
    clock: Callable[[], datetime],
    publisher: CommittedEventPublisher | None = None,
) -> CleanupCounts:
    """Run one retention pass and return counts for the caller to print.

    Captures one UTC ``now``/cutoff, then processes short per-conversation
    transactions: soft-delete purge, terminal-turn pruning, waiting-turn
    cancellation, and candidate-runtime session rotation. Pruning already
    committed the ``session_rotated`` event and cleared the native session ID,
    so a failed replacement session only marks the binding for recreation.
    """
    now = clock()
    cutoff = six_months_before(now)
    purged = await persistence.purge_soft_deleted(cutoff)

    pruned_turns = 0
    cancelled_waiting = 0
    rotated = 0
    requires_recreation = 0
    for conversation_id in await persistence.list_cleanup_conversation_ids():
        result = await persistence.prune_expired_history(conversation_id, cutoff)
        if result is None:
            continue
        pruned_turns += result.pruned_turn_count
        cancelled_waiting += result.cancelled_waiting_count
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
