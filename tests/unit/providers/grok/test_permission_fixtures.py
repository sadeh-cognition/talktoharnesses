"""Grok permission fixtures: typed actions, manual-only, options, correlation."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from talktoharnesses.domain.enums import ApprovalDecision, FileOperation
from talktoharnesses.domain.events import InteractionRequestedPayload
from talktoharnesses.domain.models import (
    ApprovalRequestPayload,
    CommandApprovalAction,
    FileApprovalAction,
    NetworkApprovalAction,
)
from talktoharnesses.providers.adapter import HarnessInteractionRequest
from talktoharnesses.providers.grok.adapter import GrokAdapter
from talktoharnesses.providers.grok.normalizer import GrokNormalizer


def _options(*kinds: str) -> list[dict[str, str]]:
    return [{"optionId": f"opt-{k}", "kind": k} for k in kinds]


def test_permission_command_argv_action() -> None:
    n = GrokNormalizer()
    n.set_session("s")
    n.begin_turn(uuid4())
    events = n.on_permission_request(
        {
            "sessionId": "s",
            "toolCall": {
                "title": "Bash",
                "rawInput": {"command": ["tool", "a", "b"]},
            },
            "options": _options("allow_once", "reject_once"),
        },
        interaction_id=uuid4(),
    )
    payload = events[0]
    assert isinstance(payload, InteractionRequestedPayload)
    assert isinstance(payload.request, ApprovalRequestPayload)
    assert isinstance(payload.request.action, CommandApprovalAction)
    assert payload.request.action.argv == ("tool", "a", "b")
    assert ApprovalDecision.ALLOW_ONCE in payload.request.available_decisions
    assert ApprovalDecision.DENY in payload.request.available_decisions
    assert ApprovalDecision.CANCEL in payload.request.available_decisions


def test_permission_file_action() -> None:
    n = GrokNormalizer()
    n.set_session("s")
    n.begin_turn(uuid4())
    events = n.on_permission_request(
        {
            "toolCall": {
                "rawInput": {"path": "/tmp/x.py", "operation": "read"},
            },
            "options": _options("allow_once", "deny_once"),
        },
        interaction_id=uuid4(),
    )
    payload = events[0]
    assert isinstance(payload, InteractionRequestedPayload)
    assert isinstance(payload.request, ApprovalRequestPayload)
    request = payload.request
    assert isinstance(request.action, FileApprovalAction)
    assert request.action.path == "/tmp/x.py"
    assert request.action.operation is FileOperation.READ


def test_permission_network_top_level() -> None:
    n = GrokNormalizer()
    n.set_session("s")
    n.begin_turn(uuid4())
    events = n.on_permission_request(
        {"networkAccess": True, "options": _options("allow_once", "reject_once")},
        interaction_id=uuid4(),
    )
    payload = events[0]
    assert isinstance(payload, InteractionRequestedPayload)
    assert isinstance(payload.request, ApprovalRequestPayload)
    assert isinstance(payload.request.action, NetworkApprovalAction)


def test_permission_network_via_raw_input() -> None:
    n = GrokNormalizer()
    n.set_session("s")
    n.begin_turn(uuid4())
    events = n.on_permission_request(
        {
            "toolCall": {"rawInput": {"network": True}},
            "options": _options("allow_once"),
        },
        interaction_id=uuid4(),
    )
    payload = events[0]
    assert isinstance(payload, InteractionRequestedPayload)
    assert isinstance(payload.request, ApprovalRequestPayload)
    assert isinstance(payload.request.action, NetworkApprovalAction)


def test_manual_only_when_no_typed_action() -> None:
    n = GrokNormalizer()
    n.set_session("s")
    n.begin_turn(uuid4())
    events = n.on_permission_request(
        {"description": "please approve this", "options": _options("allow_once")},
        interaction_id=uuid4(),
    )
    payload = events[0]
    assert isinstance(payload, InteractionRequestedPayload)
    assert isinstance(payload.request, ApprovalRequestPayload)
    request = payload.request
    assert request.action is None
    assert request.summary == "please approve this"


def test_unknown_fields_on_permission_rejected_by_schema() -> None:
    from talktoharnesses.providers.acp.schemas.base import is_allowlisted_permission_request

    assert is_allowlisted_permission_request(
        {
            "sessionId": "s",
            "toolCall": {"rawInput": {"command": ["echo"], "timeout": 60_000}},
            "options": [],
        }
    )
    assert not is_allowlisted_permission_request(
        {
            "sessionId": "s",
            "toolCall": {"rawInput": {"command": ["echo"], "shell": True}},
            "options": [],
        }
    )


@pytest.mark.asyncio
async def test_adapter_answer_rejects_unmapped_decision() -> None:
    adapter = GrokAdapter()
    adapter._normalizer.set_session("s")  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.begin_turn(uuid4())  # pyright: ignore[reportPrivateUsage]
    interaction_id = uuid4()
    adapter._pending_interactions[interaction_id] = (  # pyright: ignore[reportPrivateUsage]
        "rpc-1",
        [{"optionId": "only-allow", "kind": "allow_once"}],
    )
    from talktoharnesses.domain.errors import DomainError
    from talktoharnesses.domain.models import InteractionAnswer
    from talktoharnesses.providers.adapter import HarnessSession

    session = HarnessSession(
        conversation_id=uuid4(),
        binding_id=uuid4(),
        kind=adapter.kind,
        native_session_id="s",
    )
    adapter._session = session  # pyright: ignore[reportPrivateUsage]
    adapter._connection = SimpleNamespace(respond=lambda *a, **k: None)  # type: ignore[assignment]
    adapter._closed = False  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(DomainError):
        await adapter.answer_interaction(
            session,
            InteractionAnswer(
                interaction_id=interaction_id,
                decision=ApprovalDecision.ALLOW_SESSION,
            ),
        )
    # Waiter not popped on rejection.
    assert interaction_id in adapter._pending_interactions  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_concurrent_permission_requests_keep_distinct_waiters() -> None:
    adapter = GrokAdapter()
    adapter._normalizer.set_session("s")  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.begin_turn(uuid4())  # pyright: ignore[reportPrivateUsage]
    await adapter._on_permission_request(  # pyright: ignore[reportPrivateUsage]
        SimpleNamespace(id="rpc-1", params={"options": [], "toolCall": {"toolCallId": "t1"}})
    )
    await adapter._on_permission_request(  # pyright: ignore[reportPrivateUsage]
        SimpleNamespace(id="rpc-2", params={"options": [], "toolCall": {"toolCallId": "t2"}})
    )
    assert len(adapter._pending_interactions) == 2  # pyright: ignore[reportPrivateUsage]
    e1 = adapter._event_q.get_nowait()  # pyright: ignore[reportPrivateUsage]
    e2 = adapter._event_q.get_nowait()  # pyright: ignore[reportPrivateUsage]
    assert isinstance(e1, HarnessInteractionRequest)
    assert isinstance(e2, HarnessInteractionRequest)
    assert e1.provider_correlation["json_rpc_request_id"] == "rpc-1"
    assert e2.provider_correlation["json_rpc_request_id"] == "rpc-2"
