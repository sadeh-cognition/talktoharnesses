"""Application event-dispatch boundary tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from talktoharnesses.application.event_dispatcher import dispatch_harness_event
from talktoharnesses.domain.events import CostUpdatedPayload
from talktoharnesses.domain.transitions import new_conversation_state


def test_cost_update_is_appended_as_a_supported_harness_event() -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    state = new_conversation_state(owner_id="owner", now=now)
    payload = CostUpdatedPayload(
        turn_id=uuid4(),
        cost="0.032791776",
        currency="USD",
    )

    result = dispatch_harness_event(state, payload, now=now)

    assert result.events[0].payload == payload
    assert result.terminal is False
