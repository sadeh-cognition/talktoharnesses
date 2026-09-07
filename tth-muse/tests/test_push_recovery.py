"""The push watchdog recovers a turn whose host stopped pushing view events.

Observed on 2026-09-06 (twice): after a few hundred view events Muse's push
delivery to the subscribed connection went silent with no ``view/gap`` and no
error, while the host kept working, journaled an approval request nobody ever
saw, and answered requests normally. Paged reads of the same view kept working.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from uuid import uuid4

import pytest
from tests.test_muse import Host, start
from tth_types.adapter import HarnessInteractionRequest, HarnessSession, TurnRequest
from tth_types.events import HarnessEvent

from tth_muse.harness.adapter import MuseAdapter
from tth_muse.runtime.handle import ProcessHandle


class PagingHost(Host):
    """A host whose ``view/page`` answers from a scripted newest-first page."""

    def __init__(self) -> None:
        super().__init__()
        self.page_events: list[dict[str, Any]] = []
        self.page_requests: list[dict[str, Any]] = []
        self.resume_history: dict[str, Any] | None = None

    reject_cursor_anchor = False

    async def write_stdin(self, data: bytes) -> None:
        frame = json.loads(data)
        if (
            frame.get("method") == "session/resume"
            and self.reject_cursor_anchor
            and "cursor" in frame["params"]
        ):
            self.commands.append(frame)
            response = {
                "jsonrpc": "2.0",
                "id": frame["id"],
                "error": {
                    "code": -32011,
                    "message": "unknown cursor anchor",
                    "data": {"kind": "notFound", "reason": "missingAnchor"},
                },
            }
            await self.frames.put((json.dumps(response) + "\n").encode())
            return
        if frame.get("method") == "session/resume" and self.resume_history is not None:
            self.commands.append(frame)
            response: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": frame["id"],
                "result": {
                    "session": {"sessionId": self.session_id, "workspaceRoot": "/tmp"},
                    "history": self.resume_history,
                    "pendingRequests": [],
                    "viewCursor": f"v:{self.session_id}:99",
                },
            }
            await self.frames.put((json.dumps(response) + "\n").encode())
            return
        if frame.get("method") == "view/page":
            self.commands.append(frame)
            self.page_requests.append(frame["params"])
            response = {
                "jsonrpc": "2.0",
                "id": frame["id"],
                "result": {"events": self.page_events, "nextCursor": None},
            }
            await self.frames.put((json.dumps(response) + "\n").encode())
            return
        await super().write_stdin(data)


def _event(method: str, **params: Any) -> dict[str, Any]:
    return {"method": method, "params": params}


def _provisional_tail(host: Host, turn_id: str, item_id: str, cursor: int) -> list[dict[str, Any]]:
    """What ``view/page`` appends for a turn that is still running."""
    running = _tool(host, turn_id, item_id, "failed", cursor)
    running["item"]["reason"] = "incomplete"
    del running["item"]["visibleOutput"]
    return [
        _event("item/completed", **running),
        _event(
            "turn/completed",
            sessionId=host.session_id,
            viewCursor=f"v:{host.session_id}:{cursor + 1}",
            sourceRange={"last": {"sequence": cursor * 10}},
            turnId=turn_id,
            terminal="failed",
            reason="incomplete",
        ),
    ]


def _tool(host: Host, turn_id: str, item_id: str, status: str, cursor: int) -> dict[str, Any]:
    return {
        "sessionId": host.session_id,
        "viewCursor": f"v:{host.session_id}:{cursor}",
        "sourceRange": {"last": {"sequence": cursor * 10}},
        "item": {
            "itemId": item_id,
            "kind": "toolCall",
            "tool": "bash",
            "args": '{"command":"echo hi"}',
            "status": status,
            "turnId": turn_id,
            "visibleOutput": "hi\n",
        },
    }


async def _start_turn(
    monkeypatch: pytest.MonkeyPatch, host: PagingHost
) -> tuple[MuseAdapter, HarnessSession, str]:
    def factory() -> MuseAdapter:
        return MuseAdapter(
            push_stall_probe=0.05, push_poll_interval=0.02, push_recovery_page_limit=50
        )

    adapter, session, _ = await start(monkeypatch, adapter_factory=factory, host=host)
    await adapter.submit(session, TurnRequest(turn_id=uuid4(), prompt="work"))
    native_turn_id = next(
        frame["params"]["commandId"] for frame in host.commands if frame["method"] == "turn/start"
    )
    return adapter, session, native_turn_id


async def _drain(
    adapter: MuseAdapter, session: HarnessSession, *, count: int, timeout: float = 2.0
) -> list[HarnessEvent | HarnessInteractionRequest]:
    stream = adapter.events(session)
    out: list[HarnessEvent | HarnessInteractionRequest] = []
    for _ in range(count):
        out.append(await asyncio.wait_for(anext(stream), timeout))
    return out


def _types(events: list[HarnessEvent | HarnessInteractionRequest]) -> list[str]:
    return [
        "interaction_requested"
        if isinstance(event, HarnessInteractionRequest)
        else str(getattr(event, "type", ""))
        for event in events
    ]


async def test_dead_push_subscription_is_resubscribed_and_gap_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = PagingHost()
    host.reject_cursor_anchor = True
    adapter, session, turn = await _start_turn(monkeypatch, host)
    try:
        # One tool call arrives by push, then the host goes silent.
        await host.emit("item/started", **_tool(host, turn, "a", "inProgress", 5))
        await host.emit("item/completed", **_tool(host, turn, "a", "completed", 6))
        pushed = await _drain(adapter, session, count=3)
        assert _types(pushed) == ["tool_requested", "tool_started", "tool_completed"]

        # What the view holds meanwhile: the pushed item again (paged reads
        # renumber cursors, so it must be recognised by identity), usage, a
        # second tool call and an approval request nobody was told about.
        host.page_events = [
            _event("item/completed", **_tool(host, turn, "a", "completed", 3)),
            _event(
                "session/tokenUsage",
                sessionId=host.session_id,
                viewCursor=f"v:{host.session_id}:4",
                sourceRange={"last": {"sequence": 70}},
                promptTokens=10,
                totalTokens=12,
                usage={"outputTokens": 2, "cacheReadTokens": 1},
                turnId=turn,
            ),
            _event("item/completed", **_tool(host, turn, "b", "completed", 5)),
            _event(
                "approval/requested",
                sessionId=host.session_id,
                viewCursor=f"v:{host.session_id}:6",
                turnId=turn,
                approvalId="appr-1",
                currentRequirementId={"approvalId": "appr-1", "sourceIndex": 0},
                toolName="bash",
                rawArgs='{"command":"pip install x"}',
                availableChoices=[
                    {"choiceId": "allow_once", "decision": "approved"},
                    {"choiceId": "abort", "decision": "abort"},
                ],
            ),
            # The page also folds the still-running third tool and the open
            # turn as failed/incomplete; those must not be replayed.
            *_provisional_tail(host, turn, "c", 7),
        ]
        replayed = await _drain(adapter, session, count=5)
        assert _types(replayed) == [
            "usage_updated",
            "tool_requested",
            "tool_started",
            "tool_completed",
            "interaction_requested",
        ]
        assert adapter.push_recoveries == 1
        resumes = [frame for frame in host.commands if frame["method"] == "session/resume"]
        assert resumes[0]["params"]["cursor"] == f"v:{host.session_id}:6"
        assert resumes[0]["params"]["excludeItems"] is True
        # The host refused the pushed cursor as an anchor; the subscription
        # was re-established from the head instead.
        assert "cursor" not in resumes[1]["params"]
        assert host.page_requests[0]["direction"] == "backward"

        # The replayed approval is answerable like a pushed one.
        interaction = replayed[-1]
        assert isinstance(interaction, HarnessInteractionRequest)
        from tth_types.enums import ApprovalDecision
        from tth_types.harness import InteractionAnswer

        await adapter.answer_interaction(
            session,
            InteractionAnswer(
                interaction_id=interaction.payload.interaction_id,
                decision=ApprovalDecision.ALLOW_ONCE,
            ),
        )
        decide = next(frame for frame in host.commands if frame["method"] == "approval/decide")
        assert decide["params"]["approvalId"] == "appr-1"

        # Push comes back after the re-subscription; a terminal ends the turn
        # and the watchdog with it.
        host.page_events = []
        await host.emit(
            "turn/completed", sessionId=host.session_id, turnId=turn, terminal="completed"
        )
        (terminal,) = await _drain(adapter, session, count=1)
        assert getattr(terminal, "type", None) == "turn_completed"
        await asyncio.sleep(0.15)
        assert adapter.push_recoveries == 1
    finally:
        await adapter.close(session)


async def test_quiet_turn_with_nothing_new_in_the_view_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = PagingHost()
    adapter, session, turn = await _start_turn(monkeypatch, host)
    try:
        await host.emit("item/started", **_tool(host, turn, "slow", "inProgress", 5))
        await _drain(adapter, session, count=2)
        # A long-running tool: the view only holds what push already brought,
        # plus the fold's failed/incomplete rendering of the open turn.
        host.page_events = [
            _event("item/started", **_tool(host, turn, "slow", "inProgress", 2)),
            *_provisional_tail(host, turn, "slow", 3),
        ]
        await asyncio.sleep(0.2)
        assert host.page_requests, "the watchdog probes a silent turn"
        assert not any(frame["method"] == "session/resume" for frame in host.commands)
        assert adapter.push_recoveries == 0
        await host.emit("item/completed", **_tool(host, turn, "slow", "completed", 6))
        (done,) = await _drain(adapter, session, count=1)
        assert getattr(done, "type", None) == "tool_completed"
    finally:
        await adapter.close(session)


async def test_close_cancels_the_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    host = PagingHost()
    adapter, session, _ = await _start_turn(monkeypatch, host)
    watchdog = cast(Any, adapter)._watchdog
    assert watchdog is not None and not watchdog.done()
    await adapter.close(session)
    await asyncio.sleep(0)
    assert watchdog.cancelled() or watchdog.done()
    assert cast(ProcessHandle, cast(object, host)) is not None


def _unmapped_item(host: Host, turn_id: str, kind: str, cursor: int) -> dict[str, Any]:
    return _event(
        "item/completed",
        sessionId=host.session_id,
        viewCursor=f"v:{host.session_id}:{cursor}",
        item={"itemId": f"{kind}-{cursor}", "kind": kind, "status": "completed", "turnId": turn_id},
    )


async def test_unmapped_items_above_a_missed_message_do_not_end_the_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run 6 on 2026-09-06: the final assistant message sat under two
    ``reminderChild`` items in the page; the scan took them for "already
    forwarded", stopped, and the turn closed without its text."""
    host = PagingHost()
    adapter, session, turn = await _start_turn(monkeypatch, host)
    try:
        await host.emit("item/started", **_tool(host, turn, "a", "inProgress", 5))
        await host.emit("item/completed", **_tool(host, turn, "a", "completed", 6))
        await _drain(adapter, session, count=3)
        host.page_events = [
            _event("item/completed", **_tool(host, turn, "a", "completed", 3)),
            _event(
                "item/completed",
                sessionId=host.session_id,
                viewCursor=f"v:{host.session_id}:4",
                item={
                    "itemId": "final",
                    "kind": "agentMessage",
                    "status": "completed",
                    "turnId": turn,
                    "text": '{"completed": true, "summary": "done"}',
                },
            ),
            _unmapped_item(host, turn, "reminderChild", 5),
            _unmapped_item(host, turn, "reminderChild", 6),
            _unmapped_item(host, turn, "userMessage", 7),
            *_provisional_tail(host, turn, "zzz", 8),
        ]
        replayed = await _drain(adapter, session, count=2)
        assert _types(replayed) == ["assistant_message_started", "assistant_message_completed"]
        assert getattr(replayed[-1], "text", "") == '{"completed": true, "summary": "done"}'
        assert adapter.polling is True

        # The real terminal shows up on a later poll, with the page unchanged
        # above it; nothing is replayed twice.
        host.page_events = host.page_events[:-2] + [
            _event(
                "turn/completed",
                sessionId=host.session_id,
                viewCursor=f"v:{host.session_id}:9",
                sourceRange={"last": {"sequence": 90}},
                turnId=turn,
                terminal="completed",
                durationMs=1234,
            )
        ]
        (terminal,) = await _drain(adapter, session, count=1)
        assert getattr(terminal, "type", None) == "turn_completed"
        assert getattr(terminal, "has_assistant_message", None) is True
    finally:
        await adapter.close(session)


async def test_polling_mode_skips_repeated_resume_after_projection_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = PagingHost()
    host.resume_history = {"items": None, "mode": "none", "noneReason": "projectionUnavailable"}
    adapter, session, turn = await _start_turn(monkeypatch, host)
    try:
        await host.emit("item/started", **_tool(host, turn, "a", "inProgress", 5))
        await _drain(adapter, session, count=2)
        host.page_events = [_event("item/completed", **_tool(host, turn, "a", "completed", 3))]
        (done,) = await _drain(adapter, session, count=1)
        assert getattr(done, "type", None) == "tool_completed"
        host.page_events.append(_event("item/completed", **_tool(host, turn, "b", "completed", 4)))
        await _drain(adapter, session, count=3)
        resumes = [frame for frame in host.commands if frame["method"] == "session/resume"]
        assert len(resumes) == 1, "a host whose projection is gone is not resumed again"
        assert adapter.polling is True
    finally:
        await adapter.close(session)
