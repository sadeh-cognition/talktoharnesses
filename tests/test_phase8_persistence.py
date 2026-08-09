"""Phase 8 Work Package 1 persistence contracts (binding history, order, title)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from importlib import import_module
from typing import cast
from uuid import uuid4

import pytest
from asgiref.sync import sync_to_async
from django.apps import apps
from tests.phase8_fixtures import NOW, PROMPT, binding, commit_turn, idle_state

from talktoharnesses.application.handoff import HandoffMessage, HandoffTool
from talktoharnesses.django.models import (
    CommandRecord,
    ConversationAggregate,
    ConversationBindingRecord,
    ConversationEventRecord,
    InteractionRecord,
    MessageRecord,
    TurnRecord,
)
from talktoharnesses.django.persistence import DjangoPersistence
from talktoharnesses.domain import (
    ApprovalDecision,
    CommandKind,
    CommandStatus,
    DomainError,
    ErrorCode,
    commit_switch,
    request_interaction,
    start_turn,
    submit_turn,
    update_interaction_draft,
)
from talktoharnesses.domain.enums import InteractionKind
from talktoharnesses.domain.models import (
    ApprovalRequestPayload,
    Command,
    PendingInteraction,
    SwitchHarnessPayload,
)

# Migration modules are not importable by name (they start with a digit).
backfill = cast(
    Callable[[object, object], None],
    import_module("talktoharnesses.django.migrations.0005_phase8_backfill").backfill,
)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_commit_derives_title_and_writes_active_binding_row() -> None:
    persistence = DjangoPersistence()
    state = idle_state()
    await persistence.save_snapshot(state)

    binding_row = await ConversationBindingRecord.objects.aget(
        conversation_id=state.conversation.id
    )
    assert binding_row.binding_id == state.binding.id  # type: ignore[union-attr]
    assert binding_row.is_active is True
    assert binding_row.native_session_id == "native-1"

    await commit_turn(persistence, state, prompt=PROMPT, key="turn-1", now=NOW)

    loaded = await persistence.get_snapshot(state.conversation.id, "owner")
    assert loaded.conversation.title_derived == "one two three four five six seven eight"
    shell = await ConversationAggregate.objects.aget(conversation_id=state.conversation.id)
    assert shell.title == "one two three four five six seven eight"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_manual_title_keeps_precedence_over_derived_title() -> None:
    persistence = DjangoPersistence()
    state = idle_state()
    state = state.model_copy(
        update={"conversation": state.conversation.model_copy(update={"title_manual": "Chosen"})}
    )
    await persistence.save_snapshot(state)

    await commit_turn(persistence, state, prompt=PROMPT, key="turn-1", now=NOW)

    loaded = await persistence.get_snapshot(state.conversation.id, "owner")
    assert loaded.conversation.title_derived == "one two three four five six seven eight"
    shell = await ConversationAggregate.objects.aget(conversation_id=state.conversation.id)
    assert shell.title == "Chosen"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_events_and_commands_record_their_turn() -> None:
    persistence = DjangoPersistence()
    state = idle_state()
    await persistence.save_snapshot(state)
    after = await commit_turn(
        persistence, state, prompt=PROMPT, key="turn-1", now=NOW, assistant_text="done"
    )

    turn_id = await TurnRecord.objects.values_list("turn_id", flat=True).afirst()
    linked = [
        row.type
        async for row in ConversationEventRecord.objects.filter(turn_id=turn_id).order_by(
            "sequence"
        )
    ]
    assert linked == [
        "turn_queued",
        "turn_started",
        "assistant_message_started",
        "assistant_message_completed",
        "turn_completed",
    ]
    command_row = await CommandRecord.objects.aget(conversation_id=after.conversation.id)
    assert command_row.target_turn_id == turn_id


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_switch_closes_previous_binding_and_settles_command() -> None:
    persistence = DjangoPersistence()
    state = idle_state()
    await persistence.save_snapshot(state)
    state = await commit_turn(persistence, state, prompt=PROMPT, key="turn-1", now=NOW)

    new_binding = binding(state.conversation.id).model_copy(
        update={"id": uuid4(), "native_session_id": "native-2"}
    )
    command = Command(
        conversation_id=state.conversation.id,
        kind=CommandKind.SWITCH_HARNESS,
        idempotency_key="switch-1",
        payload=SwitchHarnessPayload(configuration=new_binding.configuration),
        created_at=NOW,
    )
    accepted = await persistence.accept_command(command)
    previous_binding_id = state.binding.id  # type: ignore[union-attr]
    switched = commit_switch(state, new_binding=new_binding, now=NOW)
    settled = accepted.model_copy(update={"status": CommandStatus.SETTLED, "settled_at": NOW})

    committed = await persistence.commit_harness_switch(
        state.conversation.id,
        state.conversation.version,
        switched.state,
        switched.events,
        command=settled,
    )

    assert [event.type for event in committed] == ["harness_switched"]
    old_row = await ConversationBindingRecord.objects.aget(binding_id=previous_binding_id)
    assert old_row.is_active is False
    assert old_row.closed_at is not None
    active_row = await ConversationBindingRecord.objects.aget(binding_id=new_binding.id)
    assert active_row.is_active is True
    assert active_row.native_session_id == "native-2"
    command_row = await CommandRecord.objects.aget(command_id=command.id)
    assert command_row.status == "settled"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_read_retained_handoff_orders_entries_and_scopes_owner() -> None:
    persistence = DjangoPersistence()
    state = idle_state()
    await persistence.save_snapshot(state)
    state = await commit_turn(
        persistence,
        state,
        prompt="first prompt",
        key="turn-1",
        now=NOW,
        assistant_text="first answer",
        tool=True,
    )
    await commit_turn(persistence, state, prompt="second prompt", key="turn-2", now=NOW)

    document = await persistence.read_retained_handoff(state.conversation.id, owner_id="owner")

    assert [
        entry.text if isinstance(entry, HandoffMessage) else entry.tool_name
        for entry in document.entries
    ] == ["first prompt", "first answer", "bash", "second prompt"]
    tool_entry = next(e for e in document.entries if isinstance(e, HandoffTool))
    assert tool_entry.output_tail == "listing"

    with pytest.raises(DomainError) as exc_info:
        await persistence.read_retained_handoff(state.conversation.id, owner_id="intruder")
    assert exc_info.value.code is ErrorCode.NOT_FOUND


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_prune_deletes_expired_turns_rotates_session_and_retitles() -> None:
    persistence = DjangoPersistence()
    state = idle_state()
    state = state.model_copy(
        update={
            "seen_native_ids": frozenset({"old-native-event"}),
            "seen_stream_offsets": frozenset({"native-1:4"}),
        }
    )
    await persistence.save_snapshot(state)
    old = NOW - timedelta(days=400)
    state = await commit_turn(
        persistence, state, prompt="stale first prompt", key="turn-1", now=old, tool=True
    )
    state = await commit_turn(
        persistence, state, prompt="fresh second prompt", key="turn-2", now=NOW
    )
    cutoff = NOW - timedelta(days=180)

    result = await persistence.prune_expired_history(state.conversation.id, cutoff)

    assert result is not None
    assert result.previous_native_session_id == "native-1"
    assert [
        entry.text for entry in result.handoff.entries if isinstance(entry, HandoffMessage)
    ] == ["fresh second prompt"]
    assert await TurnRecord.objects.acount() == 1
    assert await MessageRecord.objects.acount() == 1
    loaded = await persistence.get_snapshot(state.conversation.id, "owner")
    assert loaded.binding is not None
    assert loaded.binding.native_session_id is None
    assert loaded.binding.requires_session_recreation is True
    assert loaded.seen_native_ids == frozenset()
    assert loaded.seen_stream_offsets == frozenset()
    assert loaded.conversation.title_derived == "fresh second prompt"
    binding_row = await ConversationBindingRecord.objects.aget(
        binding_id=loaded.binding.id,
    )
    assert binding_row.native_session_id is None
    assert await persistence.prune_expired_history(state.conversation.id, cutoff) is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_rotation_commits_update_the_active_binding_row() -> None:
    persistence = DjangoPersistence()
    state = idle_state()
    assert state.binding is not None
    await persistence.save_snapshot(state)
    version = state.conversation.version

    await persistence.commit_session_rotation(
        state.conversation.id,
        version,
        native_session_id="native-2",
        launch_snapshot=None,
    )
    rotated = await ConversationBindingRecord.objects.aget(binding_id=state.binding.id)
    assert rotated.native_session_id == "native-2"
    assert rotated.requires_session_recreation is False

    await persistence.commit_rotation_requires_recreation(state.conversation.id, version)
    marked = await ConversationBindingRecord.objects.aget(binding_id=state.binding.id)
    assert marked.requires_session_recreation is True

    with pytest.raises(DomainError) as exc_info:
        await persistence.commit_rotation_requires_recreation(state.conversation.id, version + 1)
    assert exc_info.value.code is ErrorCode.OPTIMISTIC_CONFLICT


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_prune_cancels_expired_waiting_turn() -> None:
    persistence = DjangoPersistence()
    state = idle_state()
    await persistence.save_snapshot(state)
    old = NOW - timedelta(days=400)
    queued = submit_turn(state, prompt="stale prompt", idempotency_key="turn-1", now=old)
    assert queued.command is not None
    await persistence.accept_command(queued.command)
    running = start_turn(queued.state, now=old)
    interaction = PendingInteraction(
        conversation_id=state.conversation.id,
        turn_id=running.state.active_turn.id,  # type: ignore[union-attr]
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(
            available_decisions=(ApprovalDecision.ALLOW_ONCE, ApprovalDecision.CANCEL)
        ),
        created_at=old,
    )
    waiting = request_interaction(running.state, interaction, now=old)
    await persistence.commit_turn_batch(
        state.conversation.id,
        state.conversation.version,
        waiting.state,
        (*queued.events, *running.events, *waiting.events),
        (waiting.state.commands[queued.command.id],),
    )
    drafted = update_interaction_draft(
        waiting.state,
        interaction_id=interaction.id,
        draft={"answer": "private draft"},
        now=old,
    )
    await persistence.commit_turn_batch(
        state.conversation.id,
        waiting.state.conversation.version,
        drafted.state,
        drafted.events,
    )

    result = await persistence.prune_expired_history(
        state.conversation.id, NOW - timedelta(days=180)
    )

    assert result is not None
    assert result.handoff.entries == ()
    assert await TurnRecord.objects.acount() == 0
    assert await InteractionRecord.objects.acount() == 0
    assert await CommandRecord.objects.acount() == 0
    assert not await ConversationEventRecord.objects.filter(
        type="interaction_draft_updated"
    ).aexists()
    loaded = await persistence.get_snapshot(state.conversation.id, "owner")
    assert loaded.active_turn is None
    assert loaded.interactions == {}
    assert loaded.conversation.title_derived is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_backfill_recovers_links_written_by_the_runtime() -> None:
    """The 0005 data migration derives what normal commits already write."""
    persistence = DjangoPersistence()
    state = idle_state()
    await persistence.save_snapshot(state)
    await commit_turn(
        persistence, state, prompt=PROMPT, key="turn-1", now=NOW, assistant_text="answer"
    )
    expected_bindings = [
        row
        async for row in ConversationBindingRecord.objects.values_list(
            "binding_id", "native_session_id", "is_active"
        )
    ]
    expected_messages = [
        row
        async for row in MessageRecord.objects.order_by("order_index").values_list(
            "message_id", "order_index"
        )
    ]
    expected_events = [
        row
        async for row in ConversationEventRecord.objects.order_by("sequence").values_list(
            "sequence", "turn_id"
        )
    ]
    expected_commands = [
        row async for row in CommandRecord.objects.values_list("command_id", "target_turn_id")
    ]

    await ConversationBindingRecord.objects.all().adelete()
    await MessageRecord.objects.all().aupdate(order_index=0)
    await ConversationEventRecord.objects.all().aupdate(turn_id=None)
    await CommandRecord.objects.all().aupdate(target_turn_id=None)
    await sync_to_async(backfill, thread_sensitive=True)(apps, None)

    assert [
        row
        async for row in ConversationBindingRecord.objects.values_list(
            "binding_id", "native_session_id", "is_active"
        )
    ] == expected_bindings
    assert [
        row
        async for row in MessageRecord.objects.order_by("order_index").values_list(
            "message_id", "order_index"
        )
    ] == expected_messages
    assert [
        row
        async for row in ConversationEventRecord.objects.order_by("sequence").values_list(
            "sequence", "turn_id"
        )
    ] == expected_events
    assert [
        row async for row in CommandRecord.objects.values_list("command_id", "target_turn_id")
    ] == expected_commands


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_prune_skips_conversation_with_running_turn() -> None:
    persistence = DjangoPersistence()
    state = idle_state()
    await persistence.save_snapshot(state)
    old = NOW - timedelta(days=400)
    queued = submit_turn(state, prompt="stale prompt", idempotency_key="turn-1", now=old)
    assert queued.command is not None
    await persistence.accept_command(queued.command)
    running = start_turn(queued.state, now=old)
    await persistence.commit_turn_batch(
        state.conversation.id,
        state.conversation.version,
        running.state,
        (*queued.events, *running.events),
        (running.state.commands[queued.command.id],),
    )

    assert (
        await persistence.prune_expired_history(state.conversation.id, NOW - timedelta(days=180))
        is None
    )
    assert await TurnRecord.objects.acount() == 1
