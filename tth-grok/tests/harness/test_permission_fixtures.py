"""Grok permission fixtures: typed actions, manual-only, options, correlation."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from tth_types.adapter import HarnessInteractionRequest, HarnessSession
from tth_types.enums import ApprovalDecision, FileOperation
from tth_types.events import InteractionRequestedPayload
from tth_types.harness import (
    ApprovalRequestPayload,
    CommandApprovalAction,
    FileApprovalAction,
    InteractionAnswer,
    NetworkApprovalAction,
)

from tth_grok.acp.pending import PendingAcpApproval
from tth_grok.harness.adapter import GrokAdapter
from tth_grok.harness.normalizer import GrokNormalizer


def _options(*kinds: str) -> list[dict[str, str]]:
    return [{"optionId": f"opt-{k}", "kind": k} for k in kinds]


@pytest.mark.asyncio
async def test_grok_ask_user_question_blocks_until_canonical_answer() -> None:
    adapter = GrokAdapter()
    session = HarnessSession(
        conversation_id=uuid4(),
        binding_id=uuid4(),
        kind=adapter.kind,
        native_session_id="s",
    )
    adapter._session = session  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.set_session("s")  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.begin_turn(uuid4())  # pyright: ignore[reportPrivateUsage]
    responses: list[tuple[object, object]] = []

    async def respond(request_id: object, result: object) -> None:
        responses.append((request_id, result))

    adapter._connection = SimpleNamespace(respond=respond)  # type: ignore[assignment]
    await adapter._on_question_request(  # pyright: ignore[reportPrivateUsage]
        SimpleNamespace(
            id="rpc-question",
            params={
                "sessionId": "s",
                "toolCallId": "tool-question",
                "questions": [
                    {
                        "id": "style",
                        "question": "Choose a style",
                        "options": [
                            {"label": "Brief", "description": "Short"},
                            {"label": "Detailed", "description": "Long"},
                        ],
                        "multiSelect": False,
                    }
                ],
                "mode": "default",
            },
        )
    )
    interaction = adapter._event_q.get_nowait()  # pyright: ignore[reportPrivateUsage]
    assert isinstance(interaction, HarnessInteractionRequest)
    await adapter.answer_interaction(
        session,
        InteractionAnswer(
            interaction_id=interaction.payload.interaction_id,
            answers={"style": ["Brief"]},
        ),
    )
    assert responses == [
        (
            "rpc-question",
            {
                "outcome": "accepted",
                "answers": {"Choose a style": "Brief"},
                "annotations": {},
            },
        )
    ]


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
    from tth_grok.acp.schemas.base import is_allowlisted_permission_request

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
    adapter._pending_interactions[interaction_id] = PendingAcpApproval(  # pyright: ignore[reportPrivateUsage]
        rpc_id="rpc-1",
        options=({"optionId": "only-allow", "kind": "allow_once"},),
    )
    from tth_types.adapter import HarnessSession
    from tth_types.errors import DomainError
    from tth_types.harness import InteractionAnswer

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


# --- Pinned Grok 1.0.13 (5e9a58528b76) permission fixtures ------------------
#
# Captured over ACP stdio in the tth-grok sandbox on 2026-09-05, with both
# ``--always-approve`` and ``--permission-mode default``. Grok forwards its own
# tool arguments as ``rawInput`` with a ``variant`` tag, and it emits the
# permission request (with the same option set) even under ``--always-approve``.

_GROK_EDIT_OPTIONS = [
    {
        "optionId": "allow-edits-session",
        "name": "Yes, allow all edits during this session",
        "kind": "allow_always",
    },
    {"optionId": "allow-once", "name": "Yes", "kind": "allow_once"},
    {
        "optionId": "reject-once",
        "name": "No, and tell Grok what to do differently",
        "kind": "reject_once",
    },
]

_GROK_BASH_OPTIONS = [
    {
        "optionId": "always-allow",
        "name": "Yes, and don't ask again for bash commands",
        "kind": "allow_always",
    },
    {"optionId": "allow-once", "name": "Yes, proceed", "kind": "allow_once"},
    {
        "optionId": "reject-once",
        "name": "No, and tell Grok what to do differently",
        "kind": "reject_once",
    },
    {
        "optionId": "reject-always",
        "name": "No, and don't ask again for this command",
        "kind": "reject_always",
    },
]


def _grok_meta(
    name: str, kind: str, namespace: str, label: str, **tool_input: object
) -> dict[str, object]:
    return {
        "x.ai/tool": {
            "version": 1,
            "name": name,
            "kind": kind,
            "namespace": namespace,
            "label": label,
            "read_only": False,
            "input": tool_input,
        }
    }


GROK_1_0_13_WRITE_PERMISSION: dict[str, object] = {
    "sessionId": "01a071b4-6dda-7c90-a771-c54db71a695c",
    "toolCall": {
        "toolCallId": "call-7e7f35be-af2a-4c3f-ba81-1b0764fa8abd-0",
        "kind": "edit",
        "title": "Write `/work/yolo_test.md`",
        "rawInput": {
            "variant": "Write",
            "file_path": "/work/yolo_test.md",
            "content": "capture test\n",
        },
        "_meta": _grok_meta("write", "write", "opencode", "Write", path="/work/yolo_test.md"),
    },
    "options": _GROK_EDIT_OPTIONS,
}

GROK_1_0_13_SEARCH_REPLACE_PERMISSION: dict[str, object] = {
    "sessionId": "01a071b5-92fa-7710-a7a3-e106c07df457",
    "toolCall": {
        "toolCallId": "call-e4c80c1d-10ff-42fb-b1e7-c9dd34819cb1-1",
        "kind": "edit",
        "title": "Edit `/work/once_test.md`",
        "rawInput": {
            "variant": "SearchReplace",
            "file_path": "/work/once_test.md",
            "old_string": "capture test\n",
            "new_string": "capture test\nedited\n",
            "replace_all": False,
        },
        "_meta": _grok_meta(
            "search_replace", "edit", "grok_build", "Edit", path="/work/once_test.md"
        ),
    },
    "options": _GROK_EDIT_OPTIONS,
}

GROK_1_0_13_BASH_PERMISSION: dict[str, object] = {
    "sessionId": "01a071b5-92fa-7710-a7a3-e106c07df457",
    "toolCall": {
        "toolCallId": "call-a3676e0f-125f-4dc9-9618-99e3ed128996-2",
        "kind": "execute",
        "title": "Execute `echo extra >> once_test.md && cat once_test.md`",
        "rawInput": {
            "variant": "Bash",
            "command": "echo extra >> once_test.md && cat once_test.md",
            "description": "Append extra line and cat the file",
            "is_background": False,
        },
        "_meta": _grok_meta(
            "run_terminal_command",
            "execute",
            "grok_build",
            "Run Command",
            command="echo extra >> once_test.md && cat once_test.md",
            description="Append extra line and cat the file",
        ),
    },
    "options": _GROK_BASH_OPTIONS,
}


# Captured 2026-09-06 with an HTTP MCP server named ``mnemosyne`` attached via
# ``session/new``. The same request, with the same options, is emitted under
# both ``--permission-mode default`` and ``--always-approve``. Grok names the
# MCP tool ``<server>__<tool>`` in ``tool_name`` and ``title``.
_GROK_USE_TOOL_OPTIONS = [
    {"optionId": "always-allow", "name": "always allow", "kind": "allow_always"},
    {"optionId": "allow-once", "name": "allow once", "kind": "allow_once"},
    {"optionId": "reject-once", "name": "reject once", "kind": "reject_once"},
]

GROK_1_0_13_USE_TOOL_PERMISSION: dict[str, object] = {
    "sessionId": "01a0786b-229a-7d92-a8cd-962173a1e7ac",
    "toolCall": {
        "toolCallId": "call-608e0762-fcd9-4a62-af41-033ad53f483e-1",
        "kind": "other",
        "title": "mnemosyne__mnemosyne_recall",
        "rawInput": {
            "variant": "UseTool",
            "tool_name": "mnemosyne__mnemosyne_recall",
            "tool_input": {"query": "workflow description", "limit": 3},
        },
        "_meta": {
            "x.ai/tool": {
                "version": 1,
                "name": "use_tool",
                "kind": "use_tool",
                "namespace": "grok_build",
                "label": "Use Tool",
                "read_only": False,
            }
        },
    },
    "options": _GROK_USE_TOOL_OPTIONS,
}


@pytest.mark.parametrize(
    "fixture",
    [
        GROK_1_0_13_WRITE_PERMISSION,
        GROK_1_0_13_SEARCH_REPLACE_PERMISSION,
        GROK_1_0_13_BASH_PERMISSION,
        GROK_1_0_13_USE_TOOL_PERMISSION,
    ],
    ids=["write", "search_replace", "bash", "use_tool"],
)
def test_grok_1_0_13_permission_shapes_are_allowlisted(fixture: dict[str, object]) -> None:
    from tth_grok.acp.schemas.base import is_allowlisted_permission_request
    from tth_grok.acp.schemas.grok_ext import is_allowlisted_grok_permission_request

    assert is_allowlisted_grok_permission_request(fixture)
    # The edit shapes are Grok-specific; the protocol-level ACP v1 schema must
    # not silently grow to accept them.
    variant = fixture["toolCall"]["rawInput"]["variant"]  # type: ignore[index]
    assert is_allowlisted_permission_request(fixture) is (variant == "Bash")


def test_grok_write_permission_is_file_create_action() -> None:
    n = GrokNormalizer()
    n.set_session("s")
    n.begin_turn(uuid4())
    payload = n.on_permission_request(GROK_1_0_13_WRITE_PERMISSION, interaction_id=uuid4())[0]
    assert isinstance(payload, InteractionRequestedPayload)
    assert isinstance(payload.request, ApprovalRequestPayload)
    request = payload.request
    assert isinstance(request.action, FileApprovalAction)
    assert request.action.path == "/work/yolo_test.md"
    assert request.action.operation is FileOperation.CREATE
    assert request.path == "/work/yolo_test.md"
    assert request.operation is FileOperation.CREATE
    assert request.tool_name == "Write `/work/yolo_test.md`"
    # Even under --always-approve Grok advertises allow-session and allow-once,
    # which is what a yolo caller needs to auto-approve without a human.
    assert ApprovalDecision.ALLOW_SESSION in request.available_decisions
    assert ApprovalDecision.ALLOW_ONCE in request.available_decisions
    assert ApprovalDecision.DENY in request.available_decisions


def test_grok_search_replace_permission_is_file_modify_action() -> None:
    n = GrokNormalizer()
    n.set_session("s")
    n.begin_turn(uuid4())
    payload = n.on_permission_request(
        GROK_1_0_13_SEARCH_REPLACE_PERMISSION, interaction_id=uuid4()
    )[0]
    assert isinstance(payload, InteractionRequestedPayload)
    assert isinstance(payload.request, ApprovalRequestPayload)
    assert isinstance(payload.request.action, FileApprovalAction)
    assert payload.request.action.path == "/work/once_test.md"
    assert payload.request.action.operation is FileOperation.MODIFY


def test_grok_bash_permission_is_command_action() -> None:
    n = GrokNormalizer()
    n.set_session("s")
    n.begin_turn(uuid4())
    payload = n.on_permission_request(GROK_1_0_13_BASH_PERMISSION, interaction_id=uuid4())[0]
    assert isinstance(payload, InteractionRequestedPayload)
    assert isinstance(payload.request, ApprovalRequestPayload)
    assert isinstance(payload.request.action, CommandApprovalAction)
    assert payload.request.action.argv == ("echo extra >> once_test.md && cat once_test.md",)
    assert ApprovalDecision.ALLOW_SESSION in payload.request.available_decisions


def test_grok_use_tool_permission_is_named_as_mcp_tool() -> None:
    n = GrokNormalizer()
    n.set_session("s")
    n.begin_turn(uuid4())
    payload = n.on_permission_request(GROK_1_0_13_USE_TOOL_PERMISSION, interaction_id=uuid4())[0]
    assert isinstance(payload, InteractionRequestedPayload)
    assert isinstance(payload.request, ApprovalRequestPayload)
    request = payload.request
    # Fully qualified like the other adapters report MCP calls, so a caller's
    # attached-server rule matches without knowing Grok's title format.
    assert request.tool_name == "mcp__mnemosyne__mnemosyne_recall"
    # No command/file/network semantics: stays manual-only for policy rules.
    assert request.action is None
    assert request.command_args is None
    assert request.path is None
    assert ApprovalDecision.ALLOW_SESSION in request.available_decisions
    assert ApprovalDecision.ALLOW_ONCE in request.available_decisions
    assert ApprovalDecision.DENY in request.available_decisions


def test_grok_use_tool_permission_requires_object_tool_input() -> None:
    from tth_grok.acp.schemas.grok_ext import is_allowlisted_grok_permission_request

    def with_raw_input(raw_input: Mapping[str, object]) -> dict[str, object]:
        tool_call = cast(dict[str, object], GROK_1_0_13_USE_TOOL_PERMISSION["toolCall"])
        return {
            **GROK_1_0_13_USE_TOOL_PERMISSION,
            "toolCall": {**tool_call, "rawInput": dict(raw_input)},
        }

    base: dict[str, object] = {"variant": "UseTool", "tool_name": "mnemosyne__mnemosyne_recall"}
    assert is_allowlisted_grok_permission_request(with_raw_input({**base, "tool_input": {}}))
    assert not is_allowlisted_grok_permission_request(
        with_raw_input({**base, "tool_input": "query"})
    )
    assert not is_allowlisted_grok_permission_request(with_raw_input(base))
    assert not is_allowlisted_grok_permission_request(
        with_raw_input({**base, "tool_input": {}, "extra": 1})
    )


def test_grok_mcp_tool_name_only_for_use_tool_variant() -> None:
    from tth_grok.acp.schemas.grok_ext import grok_mcp_permission_tool_name

    assert (
        grok_mcp_permission_tool_name({"variant": "UseTool", "tool_name": "wiki__read_page"})
        == "mcp__wiki__read_page"
    )
    assert grok_mcp_permission_tool_name({"variant": "UseTool", "tool_name": ""}) is None
    assert grok_mcp_permission_tool_name({"variant": "UseTool"}) is None
    assert grok_mcp_permission_tool_name({"variant": "Bash", "tool_name": "x__y"}) is None


def test_grok_edit_variant_without_path_stays_manual_only() -> None:
    from tth_grok.acp.schemas.grok_ext import grok_file_permission_target

    assert grok_file_permission_target({"variant": "Write", "content": "x"}) is None
    assert grok_file_permission_target({"variant": "Unknown", "file_path": "/a"}) is None
    assert grok_file_permission_target({"variant": "Write", "file_path": ""}) is None
