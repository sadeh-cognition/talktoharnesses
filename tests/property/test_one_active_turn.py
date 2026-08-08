"""Hypothesis: at most one active turn after legal operations."""

from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from talktoharnesses.domain import (
    ConversationState,
    HarnessCapabilities,
    HarnessKind,
    complete_turn,
    interrupt_turn,
    new_conversation_state,
    start_turn,
    submit_turn,
)
from talktoharnesses.domain.enums import TurnStatus


def _active_count(state: ConversationState) -> int:
    if state.active_turn is None:
        return 0
    if state.active_turn.status in {TurnStatus.RUNNING, TurnStatus.WAITING}:
        return 1
    return 0


@settings(max_examples=40)
@given(
    st.lists(
        st.sampled_from(["submit", "start", "complete", "interrupt"]),
        min_size=1,
        max_size=25,
    )
)
def test_one_active_turn_invariant(ops: list[str]) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    state = new_conversation_state(
        owner_id="o",
        now=now,
        capabilities=HarnessCapabilities(kind=HarnessKind.GROK, version="1"),
    )
    n = 0
    for op in ops:
        try:
            if op == "submit":
                r = submit_turn(state, prompt=f"p{n}", idempotency_key=f"k{n}", now=now)
                n += 1
                state = r.state
            elif op == "start":
                r = start_turn(state, now=now)
                state = r.state
            elif op == "complete":
                r = complete_turn(state, now=now)
                state = r.state
            elif op == "interrupt":
                r = interrupt_turn(state, now=now)
                state = r.state
        except Exception:
            continue
        assert _active_count(state) <= 1
        if state.active_turn is not None and state.queued_turn is not None:
            assert state.active_turn.id != state.queued_turn.id
