"""Phase 8 retention orchestration against MemoryPersistence + FakeAdapter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from tests.runtime.conftest import FakeAdapter, make_state
from tests.runtime.memory_persistence import MemoryPersistence

from talktoharnesses.application.retention import run_cleanup, six_months_before
from talktoharnesses.domain import (
    ActivityStatus,
    BackgroundActivity,
    TurnStatus,
    append_events,
    complete_turn,
    soft_delete_conversation,
    start_turn,
    submit_turn,
)
from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.events import (
    AssistantMessageCompletedPayload,
    AssistantMessageStartedPayload,
)
from talktoharnesses.domain.transitions import ConversationState
from talktoharnesses.providers import AdapterRegistry
from talktoharnesses.runtime.manager import RuntimeManager

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
CUTOFF = six_months_before(NOW)


class _SdkFakeAdapter(FakeAdapter):
    """Retention rotation exercises the candidate path without a child process."""

    sdk_managed = True


def _registry(*, seed_reply: str = "completed") -> AdapterRegistry:
    FakeAdapter.instances.clear()
    reg = AdapterRegistry()
    reg.register(
        HarnessKind.OPENCODE,
        lambda: _SdkFakeAdapter(seed_reply=seed_reply),  # type: ignore[arg-type, return-value]
    )
    return reg


async def _seed_completed_turn(
    persistence: MemoryPersistence,
    state: ConversationState,
    *,
    completed_at: datetime,
    prompt: str,
    key: str | None = None,
) -> ConversationState:
    queued = submit_turn(
        state, prompt=prompt, idempotency_key=key or str(uuid4()), now=completed_at
    )
    assert queued.command is not None
    await persistence.accept_command(queued.command)
    running = start_turn(queued.state, now=completed_at)
    turn_id = running.state.active_turn.id  # type: ignore[union-attr]
    message_id = uuid4()
    current, streamed = append_events(
        running.state,
        completed_at,
        [
            AssistantMessageStartedPayload(turn_id=turn_id, message_id=message_id),
            AssistantMessageCompletedPayload(turn_id=turn_id, message_id=message_id, text="answer"),
        ],
    )
    completed = complete_turn(current, now=completed_at, has_assistant_message=True)
    await persistence.commit_turn_batch(
        state.conversation.id,
        state.conversation.version,
        completed.state,
        (*queued.events, *running.events, *streamed, *completed.events),
        (completed.state.commands[queued.command.id],),
    )
    stored = persistence.turns[state.conversation.id][turn_id]
    persistence.turns[state.conversation.id][turn_id] = stored.model_copy(
        update={"completed_at": completed_at, "status": TurnStatus.COMPLETED}
    )
    return await persistence.get_worker_snapshot(state.conversation.id)


@pytest.mark.asyncio
async def test_run_cleanup_prunes_expired_turn_and_rotates(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    marker = workspace / "keep.bin"
    marker.write_bytes(b"untouched-bytes")

    persistence = MemoryPersistence()
    state = make_state(now=NOW, workdir=workspace)
    assert state.binding is not None
    state = state.model_copy(
        update={
            "binding": state.binding.model_copy(update={"native_session_id": "old-native"}),
        }
    )
    persistence.seed(state)
    state = await _seed_completed_turn(
        persistence,
        state,
        completed_at=CUTOFF - timedelta(days=1),
        prompt="prune this turn",
        key="old",
    )
    # Retain a recent turn so rotation has a non-empty handoff.
    await _seed_completed_turn(
        persistence,
        state,
        completed_at=NOW - timedelta(days=1),
        prompt="keep this turn",
        key="new",
    )

    runtime = RuntimeManager(persistence, _registry(), clock=lambda: NOW)
    counts = await run_cleanup(persistence, runtime, lambda: NOW)
    assert counts.pruned_turns == 1
    assert counts.successful_rotations == 1
    assert counts.bindings_requiring_recreation == 0

    loaded = await persistence.get_worker_snapshot(state.conversation.id)
    assert loaded.binding is not None
    assert loaded.binding.native_session_id is not None
    assert loaded.binding.native_session_id != "old-native"
    assert loaded.binding.requires_session_recreation is False
    handoff = await persistence.read_retained_handoff(state.conversation.id)
    texts = [getattr(entry, "text", "") for entry in handoff.entries]
    assert not any("prune this turn" in text for text in texts)
    assert any("keep this turn" in text for text in texts)
    assert marker.read_bytes() == b"untouched-bytes"


@pytest.mark.asyncio
async def test_run_cleanup_skips_running_and_marks_failed_rotation(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    persistence = MemoryPersistence()
    running_state = make_state(now=NOW, workdir=workspace)
    persistence.seed(running_state)
    queued = submit_turn(
        running_state, prompt="active", idempotency_key="run", now=CUTOFF - timedelta(days=2)
    )
    assert queued.command is not None
    await persistence.accept_command(queued.command)
    started = start_turn(queued.state, now=CUTOFF - timedelta(days=2))
    await persistence.commit_turn_batch(
        running_state.conversation.id,
        running_state.conversation.version,
        started.state,
        (*queued.events, *started.events),
        (started.state.commands[queued.command.id],),
    )

    rotate_state = make_state(now=NOW, workdir=workspace)
    assert rotate_state.binding is not None
    rotate_state = rotate_state.model_copy(
        update={
            "binding": rotate_state.binding.model_copy(update={"native_session_id": "n2"}),
        }
    )
    persistence.seed(rotate_state)
    rotate_state = await _seed_completed_turn(
        persistence,
        rotate_state,
        completed_at=CUTOFF - timedelta(days=1),
        prompt="expired turn",
        key="exp",
    )
    await _seed_completed_turn(
        persistence,
        rotate_state,
        completed_at=NOW - timedelta(hours=1),
        prompt="retained for seed",
        key="keep",
    )

    runtime = RuntimeManager(persistence, _registry(seed_reply="failed"), clock=lambda: NOW)
    counts = await run_cleanup(persistence, runtime, lambda: NOW)
    assert counts.pruned_turns >= 1
    assert counts.bindings_requiring_recreation >= 1

    still_running = await persistence.get_worker_snapshot(running_state.conversation.id)
    assert still_running.active_turn is not None
    assert still_running.active_turn.status is TurnStatus.RUNNING

    rotated = await persistence.get_worker_snapshot(rotate_state.conversation.id)
    assert rotated.binding is not None
    assert rotated.binding.native_session_id is None
    assert rotated.binding.requires_session_recreation is True


@pytest.mark.asyncio
async def test_run_cleanup_soft_delete_purge_and_idempotent_rerun(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    persistence = MemoryPersistence()
    state = make_state(now=NOW, workdir=workspace)
    deleted_at = CUTOFF - timedelta(days=1)
    deleted = soft_delete_conversation(state, now=deleted_at)
    persistence.seed(deleted.state)

    runtime = RuntimeManager(persistence, _registry(), clock=lambda: NOW)
    first = await run_cleanup(persistence, runtime, lambda: NOW)
    assert first.purged_conversations == 1
    second = await run_cleanup(persistence, runtime, lambda: NOW)
    assert second.purged_conversations == 0
    assert state.conversation.id not in persistence.states


@pytest.mark.asyncio
async def test_run_cleanup_skips_background_active(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    persistence = MemoryPersistence()
    state = make_state(now=NOW, workdir=workspace)
    persistence.seed(state)
    state = await _seed_completed_turn(
        persistence, state, completed_at=CUTOFF - timedelta(days=1), prompt="old", key="old"
    )
    activity_id = uuid4()
    activity = BackgroundActivity(
        id=activity_id,
        conversation_id=state.conversation.id,
        parent_turn_id=next(iter(persistence.turns[state.conversation.id])),
        status=ActivityStatus.RUNNING,
        created_at=NOW,
    )
    state = state.model_copy(update={"activities": {activity_id: activity}})
    persistence.states[state.conversation.id] = state
    persistence.activities[state.conversation.id][activity_id] = activity

    runtime = RuntimeManager(persistence, _registry(), clock=lambda: NOW)
    counts = await run_cleanup(persistence, runtime, lambda: NOW)
    assert counts.pruned_turns == 0
