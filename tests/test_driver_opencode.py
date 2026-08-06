"""OpenCode driver tests against the mock HTTP/SSE server."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

import pytest

from talktoharnesses.drivers.opencode import OpenCodeHarness
from talktoharnesses.events import ContentDelta, RequestOpened, RequestResolved

FIXTURE = Path(__file__).parent / "fixtures" / "opencode_mock_server.py"


def _cmd() -> list[str]:
    return [sys.executable, str(FIXTURE)]


async def test_opencode_turn(tmp_path: Path) -> None:
    h = OpenCodeHarness(cwd=tmp_path, command=_cmd())
    try:
        session = await h.start_session()
        assert session.provider == "opencode"
        assert session.session_id.startswith("ses")
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


async def test_opencode_approval_roundtrip(tmp_path: Path) -> None:
    decisions = tmp_path / "decisions.json"
    env = {
        "TALKTOHARNESSES_OPENCODE_APPROVAL": "1",
        "TALKTOHARNESSES_OPENCODE_DECISIONS": str(decisions),
    }
    h = OpenCodeHarness(cwd=tmp_path, command=_cmd(), env=env)
    try:
        await h.start_session()
        opened: RequestOpened | None = None
        resolved: RequestResolved | None = None
        async for ev in h.send_turn("run bash"):
            if isinstance(ev, RequestOpened) and opened is None:
                opened = ev
                await h.respond(ev.request_id or "", "accept")
            if isinstance(ev, RequestResolved):
                resolved = ev
        assert opened is not None
        assert resolved is not None
        assert resolved.decision == "once"
        body = decisions.read_text(encoding="utf-8")
        assert '"reply": "once"' in body or '"once"' in body
    finally:
        await h.aclose()


async def test_resolved_is_not_emitted_when_the_server_rejects_the_reply(
    tmp_path: Path,
) -> None:
    """request.resolved must follow a successful round-trip, not precede it.

    Emitting it first reported a decision the server never accepted.
    """
    import httpx

    h = OpenCodeHarness(cwd=tmp_path, command=_cmd())
    try:
        await h.start_session()
        seen: list[str] = []

        async def watch() -> None:
            async for ev in h.stream_events():
                seen.append(ev.type)

        task = asyncio.create_task(watch())
        await asyncio.sleep(0)

        with pytest.raises(httpx.HTTPStatusError):
            await h.respond("no-such-request-id", "accept")

        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert "request.resolved" not in seen
    finally:
        await h.aclose()
