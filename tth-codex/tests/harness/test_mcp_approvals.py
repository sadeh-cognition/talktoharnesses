"""Broker the native MCP tool confirmation emitted by Codex 0.154."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tth_types.adapter import HarnessInteractionRequest, HarnessSession
from tth_types.enums import ApprovalDecision, ErrorCode, InteractionKind
from tth_types.errors import DomainError
from tth_types.harness import ApprovalRequestPayload, InteractionAnswer

from tth_codex.harness.adapter import CodexAdapter


@pytest.fixture
def elicitation() -> dict[str, Any]:
    # Captured from a real list_changes call. The tool name is only in message.
    return {
        "threadId": "thread-1",
        "turnId": "turn-1",
        "serverName": "agentbahn_evaluation",
        "mode": "form",
        "_meta": {
            "codex_approval_kind": "mcp_tool_call",
            "persist": ["session", "always"],
            "tool_description": "List this artifact's changed paths and types.",
            "tool_params": {"limit": 1},
            "tool_params_display": [{"name": "limit", "value": 1, "display_name": "limit"}],
        },
        "message": 'Allow the agentbahn_evaluation MCP server to run tool "list_changes"?',
        "requestedSchema": {"type": "object", "properties": {}},
    }


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        (ApprovalDecision.ALLOW_ONCE, {"action": "accept", "content": {}}),
        (ApprovalDecision.DENY, {"action": "decline", "content": None}),
        (ApprovalDecision.CANCEL, {"action": "cancel", "content": None}),
    ],
)
async def test_mcp_approval_waits_for_broker_decision(
    broker: tuple[CodexAdapter, HarnessSession],
    elicitation: dict[str, Any],
    decision: ApprovalDecision,
    expected: dict[str, Any],
) -> None:
    adapter, session = broker
    task = asyncio.create_task(
        asyncio.to_thread(
            adapter._approval_handler,  # pyright: ignore[reportPrivateUsage]
            "mcpServer/elicitation/request",
            elicitation,
        )
    )
    async with asyncio.timeout(2):
        async for item in adapter.events(session):
            if not isinstance(item, HarnessInteractionRequest):
                continue
            assert not task.done()
            assert item.payload.kind is InteractionKind.APPROVAL
            request = item.payload.request
            assert isinstance(request, ApprovalRequestPayload)
            assert request.tool_name == "mcp__agentbahn_evaluation__list_changes"
            assert request.summary == elicitation["message"]
            assert request.action is None
            assert request.available_decisions == (
                ApprovalDecision.ALLOW_ONCE,
                ApprovalDecision.DENY,
                ApprovalDecision.CANCEL,
            )
            assert item.provider_correlation == {"method": "mcpServer/elicitation/request"}
            await adapter.answer_interaction(
                session,
                InteractionAnswer(interaction_id=item.payload.interaction_id, decision=decision),
            )
            break
        assert await task == expected


async def test_interrupt_cancels_pending_mcp_approval(
    broker: tuple[CodexAdapter, HarnessSession], elicitation: dict[str, Any]
) -> None:
    adapter, session = broker
    task = asyncio.create_task(
        asyncio.to_thread(
            adapter._approval_handler,  # pyright: ignore[reportPrivateUsage]
            "mcpServer/elicitation/request",
            elicitation,
        )
    )
    async with asyncio.timeout(2):
        async for item in adapter.events(session):
            if isinstance(item, HarnessInteractionRequest):
                await adapter.interrupt(session)
                break
        assert await task == {"action": "cancel", "content": None}


@pytest.mark.parametrize(
    "override",
    [
        {"_meta": {}},
        {"requestedSchema": {"type": "object", "properties": {"secret": {"type": "string"}}}},
        {"message": 'Allow the unrelated MCP server to run tool "list_changes"?'},
        {"mode": "url"},
    ],
)
async def test_other_elicitations_cannot_be_mistaken_for_tool_approvals(
    broker: tuple[CodexAdapter, HarnessSession],
    elicitation: dict[str, Any],
    override: dict[str, Any],
) -> None:
    adapter, _ = broker
    with pytest.raises(DomainError) as exc:
        await asyncio.wait_for(
            asyncio.to_thread(
                adapter._approval_handler,  # pyright: ignore[reportPrivateUsage]
                "mcpServer/elicitation/request",
                elicitation | override,
            ),
            timeout=2,
        )
    assert exc.value.code is ErrorCode.UNSUPPORTED_NATIVE_EVENT
    assert not adapter._pending_interactions  # pyright: ignore[reportPrivateUsage]


def test_mcp_tool_item_is_named_as_its_approval_is() -> None:
    from openai_codex.generated.v2_all import (
        ItemStartedNotification,
        McpToolCallThreadItem,
        ThreadItem,
    )
    from openai_codex.models import Notification

    item = ThreadItem(
        root=McpToolCallThreadItem.model_validate(
            {
                "id": "mcp-1",
                "type": "mcpToolCall",
                "server": "agentbahn_evaluation",
                "tool": "list_changes",
                "arguments": {"limit": 1},
                "status": "inProgress",
            }
        )
    )
    adapter = CodexAdapter()
    coerced = adapter._coerce_notification(  # pyright: ignore[reportPrivateUsage]
        Notification(
            method="item/started",
            payload=ItemStartedNotification(
                item=item, started_at_ms=1, thread_id="thread-1", turn_id="turn-1"
            ),
        )
    )
    assert coerced is not None
    # Consumers pair the tool events with the approval by this one name.
    assert coerced["title"] == "mcp__agentbahn_evaluation__list_changes"
