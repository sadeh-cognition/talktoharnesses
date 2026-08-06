"""Cursor / Grok ACP driver tests against the mock agent peer."""

from __future__ import annotations

import sys
from pathlib import Path

from talktoharnesses.drivers.cursor import CursorHarness
from talktoharnesses.drivers.grok import GrokHarness
from talktoharnesses.events import ContentDelta, RequestOpened, RequestResolved

FIXTURE = Path(__file__).parent / "fixtures" / "acp_mock_agent.py"


def _cmd() -> list[str]:
    return [sys.executable, str(FIXTURE)]


async def test_cursor_turn_content(tmp_path: Path) -> None:
    # Skip real auth against mock: override auth_method by not calling authenticate
    # — mock accepts authenticate, but our spawn still sets cursor_login.
    h = CursorHarness(cwd=tmp_path, command=_cmd())
    # Mock agent always succeeds authenticate
    try:
        session = await h.start_session()
        assert session.provider == "cursor"
        events = [ev async for ev in h.send_turn("hi")]
        types = [e.type for e in events]
        assert "turn.started" in types
        assert "content.delta" in types
        assert "turn.completed" in types
        text = "".join(
            e.text for e in events if isinstance(e, ContentDelta) and e.content_kind == "text"
        )
        assert "OK" in text
    finally:
        await h.aclose()


async def test_grok_turn_content(tmp_path: Path) -> None:
    h = GrokHarness(cwd=tmp_path, command=_cmd())
    try:
        await h.start_session()
        events = [ev async for ev in h.send_turn("hi")]
        text = "".join(
            e.text for e in events if isinstance(e, ContentDelta) and e.content_kind == "text"
        )
        assert "OK" in text
        assert any(e.type == "turn.completed" for e in events)
    finally:
        await h.aclose()


async def test_acp_approval_roundtrip(tmp_path: Path) -> None:
    decisions = tmp_path / "decisions.json"
    env = {
        "TALKTOHARNESSES_ACP_APPROVAL": "1",
        "TALKTOHARNESSES_ACP_DECISIONS": str(decisions),
    }
    h = CursorHarness(cwd=tmp_path, command=_cmd(), env=env)
    try:
        await h.start_session()
        opened: RequestOpened | None = None
        resolved: RequestResolved | None = None
        async for ev in h.send_turn("run tool"):
            if isinstance(ev, RequestOpened) and opened is None:
                opened = ev
                await h.respond(ev.request_id or "", "accept")
            if isinstance(ev, RequestResolved):
                resolved = ev
        assert opened is not None
        assert resolved is not None
        assert resolved.decision == "allow_once"
        body = decisions.read_text(encoding="utf-8")
        assert "allow-once" in body or "selected" in body
    finally:
        await h.aclose()


async def test_normalize_unit() -> None:
    from talktoharnesses.acp.normalize import normalize_session_update

    events = normalize_session_update(
        provider="cursor",
        session_id="s1",
        thread_id="s1",
        turn_id="t1",
        update={
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "hi"},
            "messageId": "m1",
        },
    )
    assert len(events) == 1
    assert events[0].type == "content.delta"
    assert events[0].text == "hi"  # type: ignore[union-attr]
