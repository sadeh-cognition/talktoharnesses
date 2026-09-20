"""ACP session normalizer path coverage — nested content, plans, tools, usage."""

from __future__ import annotations

from uuid import uuid4

import pytest
from tth_types.enums import ApprovalDecision, ErrorCode, FileOperation, ToolOutcome
from tth_types.errors import DomainError
from tth_types.events import (
    AssistantMessageCompletedPayload,
    AssistantMessageDeltaPayload,
    AssistantMessageStartedPayload,
    InteractionRequestedPayload,
    PlanCreatedPayload,
    PlanUpdatedPayload,
    ReasoningCompletedPayload,
    ReasoningDeltaPayload,
    ReasoningStartedPayload,
    ToolCompletedPayload,
    ToolFailedPayload,
    ToolOutputDeltaPayload,
    ToolRequestedPayload,
    ToolStartedPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
    TurnInterruptedPayload,
    UsageUpdatedPayload,
)
from tth_types.harness import (
    ApprovalRequestPayload,
    CommandApprovalAction,
    FileApprovalAction,
    NetworkApprovalAction,
)

from tth_grok.acp.normalizer import (
    AcpSessionNormalizer,
    _optional_int,  # pyright: ignore[reportPrivateUsage]
)


def _n() -> AcpSessionNormalizer:
    n = AcpSessionNormalizer()
    n.set_session("sess-1")
    n.begin_turn(uuid4())
    return n


def test_resume_keeps_fresh_chunks_after_coalesced_history_replay() -> None:
    normalizer = AcpSessionNormalizer()
    normalizer.import_seen([], [f"sess-1:{index}" for index in range(1, 6)])
    normalizer.set_session("sess-1", resync=True)

    def message(text: str):
        return normalizer.on_session_update(
            {
                "sessionId": "sess-1",
                "update": {"sessionUpdate": "agent_message_chunk", "content": text},
            }
        )

    assert message("previous reply in one chunk") == []
    normalizer.set_session("sess-1", resync=False)
    normalizer.begin_turn(uuid4())
    deltas = [
        event.text
        for event in message("fresh reply")
        if isinstance(event, AssistantMessageDeltaPayload)
    ]
    assert deltas == ["fresh reply"]


def test_nested_content_shapes_and_thought_chunks() -> None:
    n = _n()
    nested = n.on_session_update(
        {
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "m1",
                "content": {
                    "content": [
                        {"type": "text", "text": "hello"},
                        {"type": "ignore", "text": "x"},
                        "not-a-block",
                    ]
                },
            },
        }
    )
    assert any(isinstance(e, AssistantMessageStartedPayload) for e in nested)
    assert any(isinstance(e, AssistantMessageDeltaPayload) and e.text == "hello" for e in nested)

    thought_dict = n.on_session_update(
        {
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "m1",
                "content": {"thought": "inner-thought"},
            },
        }
    )
    assert any(
        isinstance(e, AssistantMessageDeltaPayload) and e.text == "inner-thought"
        for e in thought_dict
    )

    top_text = n.on_session_update(
        {
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "m1",
                "text": "top-level",
            },
        }
    )
    assert any(
        isinstance(e, AssistantMessageDeltaPayload) and e.text == "top-level" for e in top_text
    )

    # Switching message ids completes the prior stream.
    switched = n.on_session_update(
        {
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "m2",
                "content": "next",
            },
        }
    )
    assert any(isinstance(e, AssistantMessageCompletedPayload) for e in switched)
    assert any(isinstance(e, AssistantMessageStartedPayload) for e in switched)

    thoughts = n.on_session_update(
        {
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"text": "thinking"},
            },
        }
    )
    assert any(isinstance(e, ReasoningStartedPayload) for e in thoughts)
    assert any(isinstance(e, ReasoningDeltaPayload) for e in thoughts)

    terminal = n.on_prompt_terminal("end_turn")
    assert any(isinstance(e, ReasoningCompletedPayload) for e in terminal)
    assert any(isinstance(e, TurnCompletedPayload) for e in terminal)


def test_plan_create_update_and_usage_optional_int() -> None:
    n = _n()
    created = n.on_session_update(
        {
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "plan",
                "entries": [
                    {"id": "a", "title": "one", "status": "pending", "detail": "d"},
                    "skip-me",
                    {"content": "untitled"},
                ],
            },
        }
    )
    assert len(created) == 1
    assert isinstance(created[0], PlanCreatedPayload)
    assert created[0].items[0].title == "one"
    assert created[0].items[1].title == "untitled"

    updated = n.on_session_update(
        {
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "plan",
                "items": [{"id": "a", "title": "two"}],
            },
        }
    )
    assert isinstance(updated[0], PlanUpdatedPayload)
    assert updated[0].plan_id == created[0].plan_id

    usage = n.on_session_update(
        {
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "usage_update",
                "inputTokens": 1,
                "output_tokens": "2",
                "totalTokens": True,
                "cached_input_tokens": "nope",
            },
        }
    )
    assert isinstance(usage[0], UsageUpdatedPayload)
    assert usage[0].input_tokens == 1
    assert usage[0].output_tokens == 2
    assert usage[0].total_tokens is None
    assert usage[0].cached_input_tokens is None


def test_tool_call_update_branches_and_redaction_lists() -> None:
    n = _n()
    n.set_redaction_patterns(("SECRET",))
    with pytest.raises(DomainError) as missing_id:
        n.on_session_update(
            {
                "sessionId": "sess-1",
                "update": {"sessionUpdate": "tool_call", "title": "x"},
            }
        )
    assert missing_id.value.code is ErrorCode.PROTOCOL_ERROR

    requested = n.on_session_update(
        {
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "t1",
                "title": "shell",
                "status": "in_progress",
                "rawInput": ["SECRET", {"token": "SECRET"}],
            },
        }
    )
    assert any(isinstance(e, ToolRequestedPayload) for e in requested)
    assert any(isinstance(e, ToolStartedPayload) for e in requested)
    req = next(e for e in requested if isinstance(e, ToolRequestedPayload))
    assert req.arguments == {"value": ["[REDACTED]", {"token": "[REDACTED]"}]}

    # Duplicate native id with known tool is ignored.
    assert (
        n.on_session_update(
            {
                "sessionId": "sess-1",
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "t1",
                    "title": "shell",
                },
            }
        )
        == []
    )

    with pytest.raises(DomainError):
        n.on_session_update(
            {
                "sessionId": "sess-1",
                "update": {"sessionUpdate": "tool_call_update", "status": "completed"},
            }
        )

    delta = n.on_session_update(
        {
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "t1",
                "content": "out-",
                "status": "running",
            },
        }
    )
    assert any(isinstance(e, ToolOutputDeltaPayload) for e in delta)
    assert any(isinstance(e, ToolStartedPayload) for e in delta)

    done = n.on_session_update(
        {
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "t1",
                "content": "ok",
                "status": "completed",
            },
        }
    )
    completed = next(e for e in done if isinstance(e, ToolCompletedPayload))
    assert completed.outcome is ToolOutcome.SUCCESS
    assert "out-ok" in completed.output_tail

    failed = n.on_session_update(
        {
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "unknown-tool",
                "status": "failed",
                "error": "boom",
            },
        }
    )
    assert any(isinstance(e, ToolFailedPayload) and e.message == "boom" for e in failed)


def test_prompt_terminals_and_protocol_edges() -> None:
    n = AcpSessionNormalizer()
    n.set_session("sess-1")
    with pytest.raises(DomainError):
        n.on_session_update(
            {
                "sessionId": "sess-1",
                "update": {"sessionUpdate": "agent_message_chunk", "content": "x"},
            }
        )

    n.begin_turn(uuid4())
    with pytest.raises(DomainError):
        n.on_session_update({"sessionId": "sess-1", "update": "not-a-dict"})
    with pytest.raises(DomainError):
        n.on_session_update({"sessionId": "sess-1", "update": {"sessionUpdate": 1}})
    with pytest.raises(DomainError) as unsupported:
        n.on_session_update(
            {
                "sessionId": "sess-1",
                "update": {"sessionUpdate": "weird_kind"},
            }
        )
    assert unsupported.value.code is ErrorCode.UNSUPPORTED_NATIVE_EVENT

    # Duplicate offset short-circuits.
    n.import_seen([], [f"sess-1:{n._stream_offset + 1}"])  # pyright: ignore[reportPrivateUsage]
    assert (
        n.on_session_update(
            {
                "sessionId": "sess-1",
                "update": {"sessionUpdate": "agent_message_chunk", "content": "x"},
            }
        )
        == []
    )

    n2 = _n()
    n2.begin_turn(uuid4())
    cancelled = n2.on_prompt_terminal("cancelled")
    assert any(isinstance(e, TurnInterruptedPayload) for e in cancelled)

    n3 = _n()
    refusal = n3.on_prompt_terminal("refusal")
    assert any(isinstance(e, TurnFailedPayload) and e.error_code == "refusal" for e in refusal)

    n4 = _n()
    failed = n4.on_prompt_terminal("exploded", error_message="nope")
    assert any(isinstance(e, TurnFailedPayload) and e.message == "nope" for e in failed)

    assert n4.note_seen_native("once") is False
    assert n4.note_seen_native("once") is True
    assert n4.note_seen_offset("off") is False
    assert n4.note_seen_offset("off") is True
    native, offsets = n4.export_seen()
    assert "once" in native and "off" in offsets


def test_permission_action_normalization_and_optional_int_edges() -> None:
    n = _n()
    options = [
        {"optionId": "allow-once", "kind": "allow_once"},
        {"optionId": "deny", "kind": "reject_once"},
    ]
    network = n.on_permission_request(
        {
            "networkAccess": True,
            "description": "net",
            "options": options,
            "toolCall": {"title": "fetch"},
        },
        interaction_id=uuid4(),
    )[0]
    assert isinstance(network, InteractionRequestedPayload)
    assert isinstance(network.request, ApprovalRequestPayload)
    assert isinstance(network.request.action, NetworkApprovalAction)
    assert ApprovalDecision.ALLOW_ONCE in network.request.available_decisions

    command = n.on_permission_request(
        {
            "options": options,
            "toolCall": {"rawInput": {"command": ["echo", "hi"]}},
        },
        interaction_id=uuid4(),
    )[0]
    assert isinstance(command, InteractionRequestedPayload)
    assert isinstance(command.request, ApprovalRequestPayload)
    assert isinstance(command.request.action, CommandApprovalAction)
    assert command.request.action.argv == ("echo", "hi")

    file_ok = n.on_permission_request(
        {
            "options": options,
            "toolCall": {"rawInput": {"path": "/tmp/a", "operation": "modify", "network": False}},
        },
        interaction_id=uuid4(),
    )[0]
    assert isinstance(file_ok, InteractionRequestedPayload)
    assert isinstance(file_ok.request, ApprovalRequestPayload)
    assert isinstance(file_ok.request.action, FileApprovalAction)
    assert file_ok.request.action.operation is FileOperation.MODIFY

    bad_op = n.on_permission_request(
        {
            "options": options,
            "toolCall": {"rawInput": {"path": "/tmp/a", "operation": "not-real"}},
        },
        interaction_id=uuid4(),
    )[0]
    assert isinstance(bad_op, InteractionRequestedPayload)
    assert isinstance(bad_op.request, ApprovalRequestPayload)
    assert bad_op.request.action is None

    assert _optional_int(None) is None  # pyright: ignore[reportPrivateUsage]
    assert _optional_int(True) is None  # pyright: ignore[reportPrivateUsage]
    assert _optional_int(7) == 7  # pyright: ignore[reportPrivateUsage]
    assert _optional_int("9") == 9  # pyright: ignore[reportPrivateUsage]
    assert _optional_int("x") is None  # pyright: ignore[reportPrivateUsage]
    assert _optional_int(object()) is None  # pyright: ignore[reportPrivateUsage]


# Captured from Grok 1.0.13 (5e9a58528b76) on 2026-09-06 with an HTTP MCP
# server named ``mnemosyne``: the first ``tool_call`` frame is titled
# ``use_tool`` with the target only in ``rawInput.tool_name``; the update
# retitles the call to the target and tags the input ``variant: UseTool``.
_GROK_USE_TOOL_META = {
    "x.ai/tool": {
        "version": 1,
        "name": "use_tool",
        "kind": "use_tool",
        "namespace": "grok_build",
        "label": "Use Tool",
        "read_only": False,
    }
}


def test_grok_use_tool_call_is_named_after_mcp_target() -> None:
    n = _n()
    requested = n.on_session_update(
        {
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "call-f3a95fff-cbcb-46a1-8bb3-2a24d12e8f27-2",
                "title": "use_tool",
                "rawInput": {
                    "tool_name": "mnemosyne__mnemosyne_recall",
                    "tool_input": {"query": "workflow description", "limit": 3},
                },
                "_meta": _GROK_USE_TOOL_META,
            },
        }
    )
    req = next(e for e in requested if isinstance(e, ToolRequestedPayload))
    assert req.tool_name == "mcp__mnemosyne__mnemosyne_recall"
    # The wrapper's arguments are kept verbatim for the detail view.
    assert req.arguments["tool_name"] == "mnemosyne__mnemosyne_recall"
    assert req.arguments["tool_input"] == {"query": "workflow description", "limit": 3}

    n.on_session_update(
        {
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call-f3a95fff-cbcb-46a1-8bb3-2a24d12e8f27-2",
                "kind": "other",
                "title": "mnemosyne__mnemosyne_recall",
                "locations": [],
                "rawInput": {
                    "variant": "UseTool",
                    "tool_name": "mnemosyne__mnemosyne_recall",
                    "tool_input": {"query": "workflow description", "limit": 3},
                },
                "_meta": _GROK_USE_TOOL_META,
            },
        }
    )
    done = n.on_session_update(
        {
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call-f3a95fff-cbcb-46a1-8bb3-2a24d12e8f27-2",
                "status": "completed",
                "rawOutput": {
                    "type": "MCP",
                    "tool_name": "mnemosyne_recall",
                    "server_name": "mnemosyne",
                    "output": {"OkayOutput": "{}"},
                },
            },
        }
    )
    completed = next(e for e in done if isinstance(e, ToolCompletedPayload))
    assert completed.tool_name == "mcp__mnemosyne__mnemosyne_recall"


def test_grok_native_tools_keep_their_own_names() -> None:
    from tth_grok.acp.schemas.grok_ext import grok_mcp_tool_call_name

    # ``search_tool`` is Grok's own MCP discovery tool, not a wrapped call.
    assert (
        grok_mcp_tool_call_name(
            {
                "title": "search_tool",
                "rawInput": {"query": "mnemosyne recall", "limit": 5},
                "_meta": {"x.ai/tool": {"name": "search_tool"}},
            }
        )
        is None
    )
    # A wrapper without a usable target keeps the wrapper name.
    assert grok_mcp_tool_call_name({"title": "use_tool", "rawInput": {"tool_input": {}}}) is None
    assert grok_mcp_tool_call_name({"title": "use_tool", "rawInput": "x"}) is None
    # The variant tag alone is enough once the update frame retitles the call.
    assert (
        grok_mcp_tool_call_name(
            {
                "title": "wiki__read_page",
                "rawInput": {"variant": "UseTool", "tool_name": "wiki__read_page"},
            }
        )
        == "mcp__wiki__read_page"
    )
