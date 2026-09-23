"""Application event-dispatch boundary tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from talktoharnesses.application.event_dispatcher import dispatch_harness_event
from talktoharnesses.domain.events import CostUpdatedPayload, HarnessEvent, ProviderWarningPayload
from talktoharnesses.domain.transitions import new_conversation_state


@pytest.mark.parametrize(
    "payload",
    [
        CostUpdatedPayload(turn_id=uuid4(), cost="0.032791776", currency="USD"),
        ProviderWarningPayload(message="Retrying provider request", code="provider_retry"),
        ProviderWarningPayload(message="Provider request failed", code="provider_error"),
    ],
)
def test_streaming_update_is_appended_without_settling_the_turn(payload: HarnessEvent) -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    state = new_conversation_state(owner_id="owner", now=now)
    result = dispatch_harness_event(state, payload, now=now)

    assert result.events[0].payload == payload
    assert result.terminal is False
