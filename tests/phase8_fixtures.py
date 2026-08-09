"""Shared Phase 8 Django fixtures: real turn commits through real projections."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from talktoharnesses.django.persistence import DjangoPersistence
from talktoharnesses.domain import (
    HarnessConfiguration,
    HarnessKind,
    ToolOutcome,
    append_events,
    complete_turn,
    new_conversation_state,
    start_turn,
    submit_turn,
)
from talktoharnesses.domain.events import (
    AssistantMessageCompletedPayload,
    AssistantMessageStartedPayload,
    ToolCompletedPayload,
    ToolRequestedPayload,
)
from talktoharnesses.domain.models import ConversationHarnessBinding
from talktoharnesses.domain.transitions import ConversationState

NOW = datetime(2026, 8, 8, tzinfo=UTC)
PROMPT = "one two three four five six seven eight nine ten"


def binding(conversation_id: UUID) -> ConversationHarnessBinding:
    return ConversationHarnessBinding(
        conversation_id=conversation_id,
        kind=HarnessKind.CODEX,
        configuration=HarnessConfiguration(kind=HarnessKind.CODEX, working_directory="/tmp"),
        native_session_id="native-1",
        created_at=NOW,
    )


def idle_state(owner: str = "owner", *, title: str | None = None) -> ConversationState:
    conversation_id = uuid4()
    state = new_conversation_state(
        owner_id=owner,
        now=NOW,
        binding=binding(conversation_id),
        conversation_id=conversation_id,
    )
    if title is None:
        return state
    return state.model_copy(
        update={"conversation": state.conversation.model_copy(update={"title_manual": title})}
    )


async def commit_turn(
    persistence: DjangoPersistence,
    state: ConversationState,
    *,
    prompt: str,
    key: str,
    now: datetime,
    assistant_text: str | None = None,
    tool: bool = False,
) -> ConversationState:
    """Queue, start, stream optional content, and complete one turn."""
    queued = submit_turn(state, prompt=prompt, idempotency_key=key, now=now)
    assert queued.command is not None
    await persistence.accept_command(queued.command)
    running = start_turn(queued.state, now=now)
    turn_id = running.state.active_turn.id  # type: ignore[union-attr]
    events = (*queued.events, *running.events)
    current = running.state
    if assistant_text is not None:
        message_id = uuid4()
        current, streamed = append_events(
            current,
            now,
            [
                AssistantMessageStartedPayload(turn_id=turn_id, message_id=message_id),
                AssistantMessageCompletedPayload(
                    turn_id=turn_id, message_id=message_id, text=assistant_text
                ),
            ],
        )
        events += streamed
    if tool:
        tool_id = uuid4()
        current, streamed = append_events(
            current,
            now,
            [
                ToolRequestedPayload(
                    turn_id=turn_id, tool_id=tool_id, tool_name="bash", arguments={"cmd": "ls"}
                ),
                ToolCompletedPayload(
                    turn_id=turn_id,
                    tool_id=tool_id,
                    tool_name="bash",
                    outcome=ToolOutcome.SUCCESS,
                    exit_status=0,
                    output_tail="listing",
                ),
            ],
        )
        events += streamed
    completed = complete_turn(current, now=now, has_assistant_message=assistant_text is not None)
    events += completed.events
    await persistence.commit_turn_batch(
        state.conversation.id,
        state.conversation.version,
        completed.state,
        events,
        (completed.state.commands[queued.command.id],),
    )
    return completed.state
