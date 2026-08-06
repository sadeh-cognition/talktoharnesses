"""Codex driver tests against the mock app-server peer."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from talktoharnesses.drivers.codex import CodexHarness
from talktoharnesses.events import (
    ContentDelta,
    RequestOpened,
    RequestResolved,
    TurnCompleted,
    TurnStarted,
)

FIXTURE_PEER = Path(__file__).parent / "fixtures" / "codex_mock_peer.py"


def _mock_command(**env: str) -> list[str]:
    return [sys.executable, str(FIXTURE_PEER)]


async def test_codex_turn_emits_content_and_completed(tmp_path: Path) -> None:
    h = CodexHarness(cwd=tmp_path, command=_mock_command())
    try:
        session = await h.start_session()
        assert session.provider == "codex"
        assert session.thread_id == "thread-test-1"

        events = [ev async for ev in h.send_turn("say hello")]
        types = [ev.type for ev in events]
        assert "turn.started" in types
        assert "content.delta" in types
        assert "turn.completed" in types

        text = "".join(
            ev.text for ev in events if isinstance(ev, ContentDelta) and ev.content_kind == "text"
        )
        assert "OK" in text
        assert any(isinstance(ev, TurnCompleted) for ev in events)
    finally:
        await h.aclose()


async def test_codex_approval_roundtrip(tmp_path: Path) -> None:
    decisions = tmp_path / "decisions.json"
    env = {
        "TALKTOHARNESSES_CODEX_APPROVAL": "1",
        "TALKTOHARNESSES_CODEX_DECISIONS": str(decisions),
    }
    h = CodexHarness(cwd=tmp_path, command=_mock_command(), env=env)
    try:
        await h.start_session()

        opened: RequestOpened | None = None
        resolved: RequestResolved | None = None
        events: list[object] = []

        async for ev in h.send_turn("run something"):
            events.append(ev)
            if isinstance(ev, RequestOpened) and opened is None:
                opened = ev
                await h.respond(ev.request_id or "", "accept")
            if isinstance(ev, RequestResolved):
                resolved = ev

        assert opened is not None
        assert opened.request_type == "command_execution"
        assert resolved is not None
        assert resolved.decision == "accept"

        # Peer recorded the JSON-RPC response with provider-native decision.
        assert decisions.exists()
        body = decisions.read_text(encoding="utf-8")
        assert "accept" in body
        assert "acceptForSession" not in body or '"decision": "accept"' in body
    finally:
        await h.aclose()


async def test_codex_accept_for_session_maps_native(tmp_path: Path) -> None:
    decisions = tmp_path / "decisions.json"
    env = {
        "TALKTOHARNESSES_CODEX_APPROVAL": "1",
        "TALKTOHARNESSES_CODEX_DECISIONS": str(decisions),
    }
    h = CodexHarness(cwd=tmp_path, command=_mock_command(), env=env)
    try:
        await h.start_session()
        async for ev in h.send_turn("run"):
            if isinstance(ev, RequestOpened):
                await h.respond(ev.request_id or "", "accept_for_session")
        body = decisions.read_text(encoding="utf-8")
        assert "acceptForSession" in body
    finally:
        await h.aclose()


async def test_send_turn_requires_session(tmp_path: Path) -> None:
    h = CodexHarness(cwd=tmp_path, command=_mock_command())
    try:
        with pytest.raises(Exception, match="start_session"):
            async for _ in h.send_turn("x"):
                pass
    finally:
        await h.aclose()


async def test_turn_started_then_completed_order(tmp_path: Path) -> None:
    h = CodexHarness(cwd=tmp_path, command=_mock_command())
    try:
        await h.start_session()
        events = [ev async for ev in h.send_turn("hi")]
        started_idx = next(i for i, e in enumerate(events) if isinstance(e, TurnStarted))
        completed_idx = next(i for i, e in enumerate(events) if isinstance(e, TurnCompleted))
        assert started_idx < completed_idx
    finally:
        await h.aclose()
