"""Hypothesis: event sequences are monotonic and gap-free."""

from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from talktoharnesses.domain import (
    append_events,
    new_conversation_state,
    start_turn,
    submit_turn,
)
from talktoharnesses.domain.events import ProviderWarningPayload


@settings(max_examples=50)
@given(st.integers(min_value=1, max_value=20))
def test_batch_sequences_monotonic(n: int) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    state = new_conversation_state(owner_id="o", now=now)
    start = state.conversation.next_event_sequence
    payloads = [ProviderWarningPayload(message=f"w{i}") for i in range(n)]
    new_state, events = append_events(state, now, payloads)
    seqs = [e.sequence for e in events]
    assert seqs == list(range(start, start + n))
    assert new_state.conversation.next_event_sequence == start + n
    assert len({e.event_id for e in events}) == n


@settings(max_examples=30)
@given(st.lists(st.text(min_size=1, max_size=20), min_size=1, max_size=8))
def test_submit_start_complete_sequences(prompts: list[str]) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    state = new_conversation_state(owner_id="o", now=now)
    seen: list[int] = []
    for i, prompt in enumerate(prompts):
        # only one active: queue then we only start if idle
        if state.active_turn is None and state.queued_turn is None:
            r = submit_turn(state, prompt=prompt, idempotency_key=f"k{i}", now=now)
            r = start_turn(r.state, now=now)
            state = r.state
            seen.extend(e.sequence for e in r.events)
        else:
            break
    assert seen == sorted(seen)
    if len(seen) >= 2:
        assert seen == list(range(seen[0], seen[0] + len(seen)))
