"""Conversation ownership fencing via MemoryPersistence (Phase 9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.runtime.memory_persistence import MemoryPersistence

from talktoharnesses.domain.enums import (
    CommandKind,
    CommandStatus,
    ConversationStatus,
    ErrorCode,
    HarnessKind,
)
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import (
    Command,
    ConversationHarnessBinding,
    HarnessConfiguration,
    SubmitTurnPayload,
)
from talktoharnesses.domain.transitions import ConversationState, new_conversation_state


def _now() -> datetime:
    return datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _seed_active(persistence: MemoryPersistence) -> tuple[ConversationState, Command]:
    now = _now()
    state = new_conversation_state(owner_id="owner", now=now)
    binding = ConversationHarnessBinding(
        conversation_id=state.conversation.id,
        kind=HarnessKind.GROK,
        configuration=HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp"),
        native_session_id="native-1",
        created_at=now,
    )
    command = Command(
        conversation_id=state.conversation.id,
        kind=CommandKind.SUBMIT_TURN,
        status=CommandStatus.ACCEPTED,
        idempotency_key="fence-1",
        payload=SubmitTurnPayload(prompt="hi"),
        created_at=now,
    )
    conversation = state.conversation.model_copy(
        update={
            "status": ConversationStatus.RUNNING,
            "current_binding_id": binding.id,
            "version": 1,
        }
    )
    state = state.model_copy(
        update={
            "binding": binding,
            "conversation": conversation,
            "commands": {command.id: command},
        }
    )
    persistence.seed(state)
    persistence.commands[command.id] = command
    persistence.accepted_queue.append(command.id)
    return state, command


@pytest.mark.asyncio
async def test_stale_fence_rejected_after_takeover() -> None:
    persistence = MemoryPersistence()
    persistence._sqlite_mode = False  # pyright: ignore[reportPrivateUsage]
    state, _command = _seed_active(persistence)
    cid = state.conversation.id

    claimed_a = await persistence.claim_commands("worker-a", 1, lease_duration=30.0)
    assert len(claimed_a) == 1
    fence_a = claimed_a[0].fence
    assert persistence.ownership[cid][0] == "worker-a"

    # Steal ownership with a higher fence.
    persistence.ownership[cid] = (
        "worker-a",
        fence_a,
        datetime.now(UTC) - timedelta(seconds=1),
    )
    ownerships = await persistence.claim_expired_conversations(
        "worker-b",
        1,
        lease_duration=30.0,
    )
    assert len(ownerships) == 1
    assert ownerships[0].fence == fence_a + 1
    assert persistence.ownership[cid][0] == "worker-b"

    stale = claimed_a[0].command.model_copy(
        update={
            "status": CommandStatus.DELIVERY_STARTED,
            "delivery_started_at": _now(),
        }
    )
    with pytest.raises(DomainError) as exc:
        await persistence.update_command(stale, worker_id="worker-a", fence=fence_a)
    assert exc.value.code is ErrorCode.STALE_OWNER

    with pytest.raises(DomainError) as exc2:
        await persistence.commit_turn_batch(
            cid,
            state.conversation.version,
            state,
            (),
            (),
            worker_id="worker-a",
            fence=fence_a,
        )
    assert exc2.value.code is ErrorCode.STALE_OWNER


@pytest.mark.asyncio
async def test_renew_and_lost_lease_behavior() -> None:
    persistence = MemoryPersistence()
    persistence._sqlite_mode = False  # pyright: ignore[reportPrivateUsage]
    state, _command = _seed_active(persistence)
    cid = state.conversation.id

    claimed = await persistence.claim_commands("worker-a", 1, lease_duration=30.0)
    fence = claimed[0].fence

    lost = await persistence.renew_owned_conversation_leases(
        "worker-a",
        lease_duration=30.0,
    )
    assert lost == ()
    assert persistence.ownership[cid][0] == "worker-a"
    assert persistence.ownership[cid][1] == fence

    # Expire the lease: renewal reports it lost and drops ownership.
    persistence.ownership[cid] = (
        "worker-a",
        fence,
        datetime.now(UTC) - timedelta(seconds=1),
    )
    lost = await persistence.renew_owned_conversation_leases(
        "worker-a",
        lease_duration=30.0,
    )
    assert len(lost) == 1
    assert lost[0].conversation_id == cid
    assert lost[0].fence == fence
    assert cid not in persistence.ownership

    # A second worker can now take over (ownership was dropped on loss).
    ownerships = await persistence.claim_expired_conversations(
        "worker-b",
        1,
        lease_duration=30.0,
    )
    assert len(ownerships) == 1
    assert ownerships[0].worker_id == "worker-b"
    assert ownerships[0].fence >= 1


@pytest.mark.asyncio
async def test_second_worker_cannot_claim_live_owned_conversation() -> None:
    persistence = MemoryPersistence()
    persistence._sqlite_mode = False  # pyright: ignore[reportPrivateUsage]
    _state, command = _seed_active(persistence)

    claimed_a = await persistence.claim_commands("worker-a", 1, lease_duration=60.0)
    assert len(claimed_a) == 1

    # Return command to accepted while A still owns the conversation lease.
    released = claimed_a[0].command.model_copy(
        update={
            "status": CommandStatus.ACCEPTED,
            "worker_id": None,
            "lease_expires_at": None,
        }
    )
    await persistence.update_command(
        released,
        worker_id="worker-a",
        fence=claimed_a[0].fence,
    )
    persistence.accepted_queue.append(command.id)

    claimed_b = await persistence.claim_commands("worker-b", 1, lease_duration=60.0)
    assert claimed_b == ()
