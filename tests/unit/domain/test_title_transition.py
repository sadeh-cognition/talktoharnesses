"""Native title projection transition."""

from __future__ import annotations

from datetime import UTC, datetime

from talktoharnesses.domain.events import ConversationTitleUpdatedPayload
from talktoharnesses.domain.transitions import apply_native_title, new_conversation_state


def test_apply_native_title() -> None:
    now = datetime.now(UTC)
    state = new_conversation_state(owner_id="o", now=now)
    result = apply_native_title(state, title_native="Hello", now=now)
    assert result.state.conversation.title_native == "Hello"
    assert len(result.events) == 1
    assert isinstance(result.events[0].payload, ConversationTitleUpdatedPayload)
    assert result.events[0].payload.title_native == "Hello"
