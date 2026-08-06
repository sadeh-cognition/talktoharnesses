"""Cross-harness conformance: same canonical event shape from mock transcripts."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from talktoharnesses.drivers.claude import ClaudeHarness
from talktoharnesses.drivers.codex import CodexHarness
from talktoharnesses.drivers.cursor import CursorHarness
from talktoharnesses.drivers.grok import GrokHarness
from talktoharnesses.drivers.opencode import OpenCodeHarness
from talktoharnesses.events import ContentDelta

FIXTURES = Path(__file__).parent / "fixtures"


def _codex(tmp_path: Path) -> CodexHarness:
    return CodexHarness(
        cwd=tmp_path,
        command=[sys.executable, str(FIXTURES / "codex_mock_peer.py")],
    )


def _cursor(tmp_path: Path) -> CursorHarness:
    return CursorHarness(
        cwd=tmp_path,
        command=[sys.executable, str(FIXTURES / "acp_mock_agent.py")],
    )


def _grok(tmp_path: Path) -> GrokHarness:
    return GrokHarness(
        cwd=tmp_path,
        command=[sys.executable, str(FIXTURES / "acp_mock_agent.py")],
    )


def _opencode(tmp_path: Path) -> OpenCodeHarness:
    return OpenCodeHarness(
        cwd=tmp_path,
        command=[sys.executable, str(FIXTURES / "opencode_mock_server.py")],
    )


def _claude(tmp_path: Path) -> ClaudeHarness:
    from collections.abc import AsyncIterator
    from dataclasses import dataclass

    @dataclass
    class _Text:
        text: str

    @dataclass
    class _Assistant:
        content: list[Any]
        model: str = "claude-test"
        message_id: str | None = "msg-1"
        parent_tool_use_id: str | None = None
        error: str | None = None
        usage: dict[str, Any] | None = None
        stop_reason: str | None = "end_turn"
        session_id: str | None = "sess-1"
        uuid: str | None = None

    @dataclass
    class _Result:
        subtype: str = "success"
        duration_ms: int = 1
        duration_api_ms: int = 1
        is_error: bool = False
        num_turns: int = 1
        session_id: str = "sess-1"
        stop_reason: str | None = "end_turn"
        total_cost_usd: float | None = 0.0
        usage: dict[str, Any] | None = None
        result: str | None = "OK"
        structured_output: Any = None
        model_usage: dict[str, Any] | None = None
        permission_denials: list[Any] | None = None
        deferred_tool_use: Any = None
        errors: list[str] | None = None
        api_error_status: int | None = None
        uuid: str | None = None
        terminal_reason: str | None = None

    class _Fake:
        def __init__(self, options: Any = None) -> None:
            self.options = options
            self.can_use_tool = getattr(options, "can_use_tool", None)

        async def connect(self, prompt: Any = None) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        async def query(self, prompt: str, session_id: str = "default") -> None:
            return None

        async def receive_response(self) -> AsyncIterator[Any]:
            yield _Assistant(content=[_Text("Hel"), _Text("lo OK")])
            yield _Result()

        async def interrupt(self) -> None:
            return None

    return ClaudeHarness(cwd=tmp_path, client_factory=_Fake)


HARNESS_BUILDERS = {
    "codex": _codex,
    "cursor": _cursor,
    "grok": _grok,
    "opencode": _opencode,
    "claude": _claude,
}


@pytest.mark.parametrize("name", sorted(HARNESS_BUILDERS))
async def test_canonical_turn_shape(name: str, tmp_path: Path) -> None:
    """Every driver yields turn.started → content.delta* → turn.completed with OK."""
    h = HARNESS_BUILDERS[name](tmp_path)
    try:
        session = await h.start_session()
        assert session.provider == name
        events = [ev async for ev in h.send_turn("reply with OK")]
        types = [e.type for e in events]

        assert types[0] == "turn.started" or "turn.started" in types
        assert "content.delta" in types
        assert "turn.completed" in types

        # Ordering: first turn.started before last turn.completed
        started = types.index("turn.started")
        completed = len(types) - 1 - types[::-1].index("turn.completed")
        assert started < completed

        # At least one content.delta between them
        mid = types[started : completed + 1]
        assert "content.delta" in mid

        text = "".join(
            e.text for e in events if isinstance(e, ContentDelta) and e.content_kind == "text"
        )
        assert "OK" in text

        # Events carry provider and thread linkage
        for e in events:
            assert e.provider == name
    finally:
        await h.aclose()
