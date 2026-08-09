"""Cursor adapter lifecycle tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from talktoharnesses.domain.enums import ApprovalDecision, ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import TurnFailedPayload, TurnOutcomeUnknownPayload
from talktoharnesses.domain.models import InteractionAnswer
from talktoharnesses.providers.acp.jsonrpc import JsonRpcRemoteError
from talktoharnesses.providers.adapter import HarnessInteractionRequest, HarnessSession
from talktoharnesses.providers.cursor.adapter import CursorAdapter


@pytest.mark.asyncio
async def test_prompt_protocol_failure_publishes_unknown_before_stream_close() -> None:
    adapter = CursorAdapter()
    turn_id = uuid4()
    adapter._normalizer.set_session("cursor-session")  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.begin_turn(turn_id)  # pyright: ignore[reportPrivateUsage]

    await adapter._emit_prompt_outcome_unknown_and_close(  # pyright: ignore[reportPrivateUsage]
        "connection closed"
    )

    event = await adapter._event_q.get()  # pyright: ignore[reportPrivateUsage]
    end = await adapter._event_q.get()  # pyright: ignore[reportPrivateUsage]
    assert isinstance(event, TurnOutcomeUnknownPayload)
    assert event.turn_id == turn_id
    assert event.delivery_phase == "delivered"
    assert end is None


@pytest.mark.asyncio
async def test_permission_request_and_answer_interaction() -> None:
    adapter = CursorAdapter()
    session = HarnessSession(
        conversation_id=uuid4(),
        binding_id=uuid4(),
        kind=HarnessKind.CURSOR,
        native_session_id="cursor-session",
    )
    adapter._session = session  # pyright: ignore[reportPrivateUsage]
    adapter._closed = False  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.set_session("cursor-session")  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.begin_turn(uuid4())  # pyright: ignore[reportPrivateUsage]
    responded: list[tuple[object, object]] = []

    async def respond(rpc_id: object, result: object) -> None:
        responded.append((rpc_id, result))

    adapter._connection = SimpleNamespace(respond=respond)  # type: ignore[assignment]

    await adapter._on_permission_request(  # pyright: ignore[reportPrivateUsage]
        SimpleNamespace(
            id="rpc-9",
            params={
                "sessionId": "cursor-session",
                "toolCall": {"toolCallId": "tool-1"},
                "options": [{"optionId": "allow-once", "kind": "allow_once"}],
            },
        )
    )
    event = adapter._event_q.get_nowait()  # pyright: ignore[reportPrivateUsage]
    assert isinstance(event, HarnessInteractionRequest)
    assert event.provider_correlation == {
        "json_rpc_request_id": "rpc-9",
        "tool_call_id": "tool-1",
        "native_session_id": "cursor-session",
    }
    interaction_id = event.payload.interaction_id

    await adapter.answer_interaction(
        session,
        InteractionAnswer(interaction_id=interaction_id, decision=ApprovalDecision.ALLOW_ONCE),
    )
    assert responded == [("rpc-9", {"outcome": {"outcome": "selected", "optionId": "allow-once"}})]
    assert interaction_id not in adapter._pending_interactions  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(DomainError) as exc:
        await adapter.answer_interaction(
            session,
            InteractionAnswer(interaction_id=uuid4(), decision=ApprovalDecision.ALLOW_ONCE),
        )
    assert exc.value.code is ErrorCode.INVALID_STATE


@pytest.mark.asyncio
async def test_close_cancels_pending_and_watch_prompt_branches() -> None:
    adapter = CursorAdapter()
    session = HarnessSession(
        conversation_id=uuid4(),
        binding_id=uuid4(),
        kind=HarnessKind.CURSOR,
        native_session_id="cursor-session",
    )
    adapter._session = session  # pyright: ignore[reportPrivateUsage]
    adapter._closed = False  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.set_session("cursor-session")  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.begin_turn(uuid4())  # pyright: ignore[reportPrivateUsage]

    notified: list[tuple[str, object]] = []
    responded: list[tuple[object, object]] = []

    async def respond(rpc_id: object, result: object) -> None:
        responded.append((rpc_id, result))

    async def notify(method: str, params: object) -> None:
        notified.append((method, params))

    async def close() -> None:
        return None

    adapter._connection = SimpleNamespace(  # type: ignore[assignment]
        respond=respond,
        notify=notify,
        close=close,
    )
    adapter._pending_interactions[uuid4()] = (  # pyright: ignore[reportPrivateUsage]
        "rpc-pending",
        [{"optionId": "allow-once", "kind": "allow_once"}],
    )

    # Unmapped decision rejected before native respond.
    await adapter._on_permission_request(  # pyright: ignore[reportPrivateUsage]
        SimpleNamespace(
            id="rpc-map",
            params={
                "sessionId": "cursor-session",
                "options": [{"optionId": "allow-once", "kind": "allow_once"}],
            },
        )
    )
    event = adapter._event_q.get_nowait()  # pyright: ignore[reportPrivateUsage]
    assert isinstance(event, HarnessInteractionRequest)
    with pytest.raises(DomainError) as unmapped:
        await adapter.answer_interaction(
            session,
            InteractionAnswer(
                interaction_id=event.payload.interaction_id,
                decision=ApprovalDecision.ALLOW_SESSION,
            ),
        )
    assert unmapped.value.code is ErrorCode.INVALID_STATE

    await adapter.interrupt(session)
    assert any(method == "session/cancel" for method, _ in notified)
    assert any(rpc_id == "rpc-pending" for rpc_id, _ in responded)

    # watch_prompt error branches
    adapter._closed = False  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.begin_turn(uuid4())  # pyright: ignore[reportPrivateUsage]
    remote = asyncio.get_running_loop().create_future()
    remote.set_exception(JsonRpcRemoteError(code=-1, message="remote boom"))
    await adapter._watch_prompt(remote)  # pyright: ignore[reportPrivateUsage]
    failed = adapter._event_q.get_nowait()  # pyright: ignore[reportPrivateUsage]
    assert isinstance(failed, TurnFailedPayload)

    adapter._normalizer.begin_turn(uuid4())  # pyright: ignore[reportPrivateUsage]
    generic = asyncio.get_running_loop().create_future()
    generic.set_exception(RuntimeError("explode"))
    await adapter._watch_prompt(generic)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(adapter._event_q.get_nowait(), TurnFailedPayload)  # pyright: ignore[reportPrivateUsage]

    adapter._normalizer.begin_turn(uuid4())  # pyright: ignore[reportPrivateUsage]
    ok = asyncio.get_running_loop().create_future()
    ok.set_result({"stopReason": "end_turn"})
    await adapter._watch_prompt(ok)  # pyright: ignore[reportPrivateUsage]

    await adapter.close(session)
    await adapter.close(session)  # idempotent
    with pytest.raises(DomainError):
        adapter._require_session(session)  # pyright: ignore[reportPrivateUsage]
