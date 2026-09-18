"""Native Codex error notifications must preserve retries and terminal errors."""

from __future__ import annotations

from uuid import uuid4

import pytest
from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification,
    ErrorNotification,
    Turn,
    TurnCompletedNotification,
    TurnError,
    TurnStatus,
)
from openai_codex.models import Notification
from tth_types.adapter import HarnessInteractionRequest
from tth_types.events import (
    AssistantMessageDeltaPayload,
    HarnessEvent,
    ProviderWarningPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
)

from tth_codex.harness.adapter import CodexAdapter

# Exercise the adapter's native event boundary without a live model request.
# pyright: reportPrivateUsage=false


@pytest.mark.parametrize("will_retry", [True, False])
async def test_sdk_error_preserves_native_retry_and_terminal_outcome(will_retry: bool) -> None:
    adapter = CodexAdapter()
    adapter._normalizer.set_session("thread-1")
    turn_id = uuid4()
    adapter._normalizer.begin_turn(turn_id)
    error = TurnError(message="Selected model is at capacity. Please try a different model.")
    await adapter._handle_native_event(
        Notification(
            method="error",
            payload=ErrorNotification(
                error=error,
                thread_id="thread-1",
                turn_id="turn-1",
                will_retry=will_retry,
            ),
        )
    )
    warning = adapter._event_q.get_nowait()
    assert isinstance(warning, ProviderWarningPayload)
    assert warning.message == error.message
    assert warning.code == ("provider_retry" if will_retry else "provider_error")
    assert adapter._event_q.empty()

    if will_retry:
        await adapter._handle_native_event(
            Notification(
                method="item/agentMessage/delta",
                payload=AgentMessageDeltaNotification(
                    delta="Recovered", item_id="item-1", thread_id="thread-1", turn_id="turn-1"
                ),
            )
        )
    completed = Notification(
        method="turn/completed",
        payload=TurnCompletedNotification(
            thread_id="thread-1",
            turn=Turn(
                id="turn-1",
                items=[],
                items_view=None,
                status=TurnStatus.completed if will_retry else TurnStatus.failed,
                error=None if will_retry else error,
            ),
        ),
    )
    await adapter._handle_native_event(completed)
    await adapter._handle_native_event(completed)
    events: list[HarnessEvent | HarnessInteractionRequest | None] = []
    while not adapter._event_q.empty():
        events.append(adapter._event_q.get_nowait())
    terminals = [e for e in events if isinstance(e, (TurnCompletedPayload, TurnFailedPayload))]
    assert len(terminals) == 1
    terminal = terminals[0]
    assert terminal.turn_id == turn_id
    if will_retry:
        assert isinstance(terminal, TurnCompletedPayload)
        assert any(
            isinstance(e, AssistantMessageDeltaPayload) and e.text == "Recovered" for e in events
        )
    else:
        assert isinstance(terminal, TurnFailedPayload)
        assert terminal.error_code == "provider_error"
        assert terminal.message == error.message
