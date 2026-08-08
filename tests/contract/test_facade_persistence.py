"""Owner-scoped projection persistence contracts (Memory + Django)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from tests.runtime.memory_persistence import MemoryPersistence

from talktoharnesses.domain import (
    DomainError,
    ErrorCode,
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessKind,
    append_events,
    archive_conversation,
    new_conversation_state,
    pin_conversation,
    soft_delete_conversation,
    start_turn,
    submit_turn,
)
from talktoharnesses.domain.events import (
    ActivityStartedPayload,
    AssistantMessageCompletedPayload,
    AssistantMessageStartedPayload,
    PlanCreatedPayload,
    ToolRequestedPayload,
)
from talktoharnesses.domain.models import ConversationHarnessBinding, HarnessInstance, PlanItem
from talktoharnesses.domain.transitions import ConversationState


def _now() -> datetime:
    return datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


def _binding(conversation_id: UUID, *, model: str = "grok") -> ConversationHarnessBinding:
    return ConversationHarnessBinding(
        conversation_id=conversation_id,
        kind=HarnessKind.GROK,
        configuration=HarnessConfiguration(
            kind=HarnessKind.GROK,
            working_directory="/tmp/ws",
            model=model,
            mode="default",
        ),
        created_at=_now(),
    )


def _idle(owner: str = "owner-a") -> ConversationState:
    cid = uuid4()
    binding = _binding(cid)
    state = new_conversation_state(
        owner_id=owner,
        now=_now(),
        binding=binding,
        conversation_id=cid,
        capabilities=HarnessCapabilities(kind=HarnessKind.GROK, version="1.0.0"),
    )
    return state.model_copy(
        update={
            "conversation": state.conversation.model_copy(
                update={"current_binding_id": binding.id, "title_manual": "Alpha chat"}
            )
        }
    )


async def _seed_two_owners(
    persistence: MemoryPersistence,
) -> tuple[ConversationState, ConversationState]:
    a = _idle("owner-a")
    b = _idle("owner-b")
    b = b.model_copy(
        update={"conversation": b.conversation.model_copy(update={"title_manual": "Beta chat"})}
    )
    await persistence.save_snapshot(a)
    await persistence.save_snapshot(b)
    return a, b


@pytest.mark.asyncio
async def test_memory_owner_isolation_list_and_get() -> None:
    p = MemoryPersistence()
    a, b = await _seed_two_owners(p)

    page = await p.list_conversations("owner-a", limit=50)
    assert {s.id for s in page.items} == {a.conversation.id}
    assert all(s.title for s in page.items)

    with pytest.raises(DomainError) as exc:
        await p.get_conversation_snapshot(b.conversation.id, "owner-a")
    assert exc.value.code is ErrorCode.NOT_FOUND

    with pytest.raises(DomainError) as exc2:
        await p.get_high_water_sequence(b.conversation.id, "owner-a")
    assert exc2.value.code is ErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_memory_cursor_pagination_and_invalid() -> None:
    p = MemoryPersistence()
    states: list[ConversationState] = []
    for i in range(3):
        s = _idle("owner")
        s = s.model_copy(
            update={
                "conversation": s.conversation.model_copy(
                    update={
                        "title_manual": f"Chat {i}",
                        "updated_at": _now() + timedelta(minutes=i),
                    }
                )
            }
        )
        await p.save_snapshot(s)
        states.append(s)

    first = await p.list_conversations("owner", limit=2)
    assert len(first.items) == 2
    assert first.next_cursor is not None
    second = await p.list_conversations("owner", cursor=first.next_cursor, limit=2)
    assert len(second.items) == 1
    assert second.next_cursor is None
    # No overlap.
    assert {i.id for i in first.items}.isdisjoint({i.id for i in second.items})

    with pytest.raises(DomainError) as exc:
        await p.list_conversations("owner", cursor="%%%")
    assert exc.value.code is ErrorCode.INVALID_CURSOR


@pytest.mark.asyncio
async def test_memory_search_and_soft_delete() -> None:
    p = MemoryPersistence()
    a, _b = await _seed_two_owners(p)
    # Inject searchable message via memory store.
    from talktoharnesses.domain.enums import MessageRole
    from talktoharnesses.domain.models import Message

    turn_id = uuid4()
    msg = Message(
        turn_id=turn_id,
        role=MessageRole.USER,
        text="unique-search-token-xyz",
        created_at=_now(),
    )
    p.messages[a.conversation.id] = {msg.id: msg}
    p._refresh_search(  # pyright: ignore[reportPrivateUsage]
        p.states[a.conversation.id]
    )

    hits = await p.search_conversations("owner-a", "unique-search-token")
    assert len(hits.items) == 1
    assert hits.items[0].id == a.conversation.id

    # Other owner cannot see it.
    other = await p.search_conversations("owner-b", "unique-search-token")
    assert other.items == ()

    result = soft_delete_conversation(p.states[a.conversation.id], now=_now())
    await p.commit_facade_mutation(
        a.conversation.id,
        "owner-a",
        a.conversation.version,
        result.state,
        result.events,
    )
    listed = await p.list_conversations("owner-a")
    assert listed.items == ()
    with pytest.raises(DomainError) as exc:
        await p.get_conversation_snapshot(a.conversation.id, "owner-a")
    assert exc.value.code is ErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_memory_facade_mutation_metadata_and_harness() -> None:
    p = MemoryPersistence()
    state = _idle("owner-a")
    await p.save_snapshot(state)

    pinned = pin_conversation(state, now=_now())
    events = await p.commit_facade_mutation(
        state.conversation.id,
        "owner-a",
        state.conversation.version,
        pinned.state,
        pinned.events,
    )
    assert events and events[0].type == "conversation_metadata_changed"
    snap = await p.get_conversation_snapshot(state.conversation.id, "owner-a")
    assert snap.detail.conversation.pinned_at is not None
    assert snap.sequence >= 1

    harness = HarnessInstance(
        owner_id="owner-a",
        name="local-grok",
        kind=HarnessKind.GROK,
        configuration=HarnessConfiguration(
            kind=HarnessKind.GROK,
            working_directory="/tmp",
        ),
        created_at=_now(),
    )
    proj = await p.create_harness(harness)
    assert proj.name == "local-grok"
    listed = await p.list_harnesses("owner-a")
    assert len(listed.items) == 1
    with pytest.raises(DomainError):
        await p.get_harness(harness.id, "owner-b")

    caps = HarnessCapabilities(kind=HarnessKind.GROK, version="1.0.0")
    probe = await p.save_harness_probe(harness.id, "owner-a", caps, probed_at=_now())
    assert probe.capabilities.version == "1.0.0"
    loaded = await p.get_harness_probe(harness.id, "owner-a")
    assert loaded.capabilities.version == "1.0.0"


@pytest.mark.asyncio
async def test_memory_snapshot_user_anchored_turns() -> None:
    p = MemoryPersistence()
    state = _idle("owner")
    await p.save_snapshot(state)
    # Create 25 user-anchored turns; snapshot should keep newest 20.
    from talktoharnesses.domain.enums import MessageRole, TurnStatus
    from talktoharnesses.domain.models import Message, Turn

    for i in range(25):
        turn_id = uuid4()
        msg_id = uuid4()
        turn = Turn(
            id=turn_id,
            conversation_id=state.conversation.id,
            status=TurnStatus.COMPLETED,
            user_message_id=msg_id,
            created_at=_now() + timedelta(seconds=i),
        )
        msg = Message(
            id=msg_id,
            turn_id=turn_id,
            role=MessageRole.USER,
            text=f"msg {i}",
            created_at=turn.created_at,
        )
        p.turns[state.conversation.id][turn_id] = turn
        p.turn_order[state.conversation.id].append(turn_id)
        p.messages[state.conversation.id][msg_id] = msg
        # Tool-only turn without user message should not push out user turns.
        tool_turn_id = uuid4()
        p.turns[state.conversation.id][tool_turn_id] = Turn(
            id=tool_turn_id,
            conversation_id=state.conversation.id,
            status=TurnStatus.COMPLETED,
            user_message_id=None,
            created_at=_now() + timedelta(seconds=i, milliseconds=1),
        )
        p.turn_order[state.conversation.id].append(tool_turn_id)

    snap = await p.get_conversation_snapshot(state.conversation.id, "owner")
    assert len(snap.detail.turns) == 20
    assert all(t.user_message_id is not None for t in snap.detail.turns)
    # Newest 20 user turns: indices 5..24
    texts: list[str] = []
    for t in snap.detail.turns:
        m = p.messages[state.conversation.id][t.user_message_id]  # type: ignore[index]
        texts.append(m.text)
    assert texts[0] == "msg 5"
    assert texts[-1] == "msg 24"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_django_list_search_owner_isolation_and_cursors() -> None:
    from talktoharnesses.django.persistence import DjangoPersistence

    p = DjangoPersistence()
    a = _idle("owner-a")
    b = _idle("owner-b")
    await p.save_snapshot(a)
    await p.save_snapshot(b)

    # Mutate title into search doc via materialize after pin (refreshes search).
    pinned = pin_conversation(a, now=_now())
    await p.commit_facade_mutation(
        a.conversation.id,
        "owner-a",
        a.conversation.version,
        pinned.state,
        pinned.events,
    )

    page = await p.list_conversations("owner-a")
    assert {s.id for s in page.items} == {a.conversation.id}

    with pytest.raises(DomainError) as exc:
        await p.get_conversation_snapshot(b.conversation.id, "owner-a")
    assert exc.value.code is ErrorCode.NOT_FOUND

    harness = HarnessInstance(
        owner_id="owner-a",
        name="h1",
        kind=HarnessKind.GROK,
        configuration=HarnessConfiguration(
            kind=HarnessKind.GROK,
            working_directory="/tmp",
        ),
        created_at=_now(),
    )
    await p.create_harness(harness)
    listed = await p.list_harnesses("owner-b")
    assert listed.items == ()

    archived = archive_conversation(pinned.state, now=_now())
    await p.commit_facade_mutation(
        a.conversation.id,
        "owner-a",
        pinned.state.conversation.version,
        archived.state,
        archived.events,
    )
    still = await p.list_conversations("owner-a", include_archived=False)
    assert still.items == ()
    with_arch = await p.list_conversations("owner-a", include_archived=True)
    assert len(with_arch.items) == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_django_facade_mutation_creates_command_and_events() -> None:
    from talktoharnesses.django.persistence import DjangoPersistence

    p = DjangoPersistence()
    state = _idle("owner")
    await p.save_snapshot(state)
    result = submit_turn(state, prompt="hello", idempotency_key="k1", now=_now())
    events = await p.commit_facade_mutation(
        state.conversation.id,
        "owner",
        state.conversation.version,
        result.state,
        result.events,
        commands=(result.command,) if result.command else (),
    )
    assert events
    loaded = await p.get_snapshot(state.conversation.id, "owner")
    assert loaded.conversation.version == result.state.conversation.version
    snap = await p.get_conversation_snapshot(state.conversation.id, "owner")
    assert snap.sequence == max(0, result.state.conversation.next_event_sequence - 1)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_django_snapshot_includes_selected_turn_children_in_canonical_order() -> None:
    from talktoharnesses.django.persistence import DjangoPersistence

    p = DjangoPersistence()
    state = _idle("owner")
    await p.save_snapshot(state)
    queued = submit_turn(state, prompt="hello", idempotency_key="k1", now=_now())
    assert queued.command is not None
    await p.commit_facade_mutation(
        state.conversation.id,
        "owner",
        state.conversation.version,
        queued.state,
        queued.events,
        commands=(queued.command,),
    )
    started = start_turn(queued.state, now=_now())
    await p.commit_facade_mutation(
        state.conversation.id,
        "owner",
        queued.state.conversation.version,
        started.state,
        started.events,
        commands=tuple(started.state.commands.values()),
    )
    assert started.state.active_turn is not None
    turn_id = started.state.active_turn.id
    message_id = uuid4()
    first_tool_id = UUID(int=2)
    second_tool_id = UUID(int=1)
    first_plan_id = UUID(int=4)
    second_plan_id = UUID(int=3)
    projected, events = append_events(
        started.state,
        _now() + timedelta(seconds=1),
        [
            AssistantMessageStartedPayload(turn_id=turn_id, message_id=message_id),
            AssistantMessageCompletedPayload(
                turn_id=turn_id,
                message_id=message_id,
                text="response",
            ),
            ToolRequestedPayload(
                turn_id=turn_id,
                tool_id=first_tool_id,
                tool_name="first",
            ),
            ToolRequestedPayload(
                turn_id=turn_id,
                tool_id=second_tool_id,
                tool_name="second",
            ),
            PlanCreatedPayload(
                turn_id=turn_id,
                plan_id=first_plan_id,
                items=(PlanItem(id="1", title="first"),),
            ),
            PlanCreatedPayload(
                turn_id=turn_id,
                plan_id=second_plan_id,
                items=(PlanItem(id="2", title="second"),),
            ),
            ActivityStartedPayload(
                activity_id=uuid4(),
                parent_turn_id=turn_id,
                title="child",
            ),
        ],
    )
    await p.commit_facade_mutation(
        state.conversation.id,
        "owner",
        started.state.conversation.version,
        projected,
        events,
    )

    snapshot = await p.get_conversation_snapshot(state.conversation.id, "owner")
    assert len(snapshot.detail.turns) == 1
    assert snapshot.detail.turns[0].user_message_id is not None
    assert [message.text for message in snapshot.detail.messages] == ["hello", "response"]
    assert [tool.id for tool in snapshot.detail.tools] == [first_tool_id, second_tool_id]
    assert [plan.id for plan in snapshot.detail.plans] == [first_plan_id, second_plan_id]
    assert len(snapshot.detail.activity) == 1

    tool_page = await p.page_tools(state.conversation.id, "owner")
    plan_page = await p.page_plans(state.conversation.id, "owner")
    assert [tool.id for tool in tool_page.items] == [second_tool_id, first_tool_id]
    assert [plan.id for plan in plan_page.items] == [second_plan_id, first_plan_id]
