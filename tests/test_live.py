"""Opt-in live smoke tests against real agent CLIs.

Skipped unless the binary is on PATH. Run with::

    pytest -m live
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from talktoharnesses.events import ContentDelta
from talktoharnesses.registry import create_harness, ensure_drivers_loaded

PROMPT = "reply with the word OK and nothing else"


def _has(binary: str) -> bool:
    return shutil.which(binary) is not None


def _skip_unless(binary: str, *extra_checks: bool) -> None:
    if not _has(binary):
        pytest.skip(f"{binary!r} not on PATH")
    for ok in extra_checks:
        if not ok:
            pytest.skip("harness not authenticated / preconditions unmet")


@pytest.mark.live
async def test_live_codex(tmp_path: Path) -> None:
    _skip_unless("codex")
    ensure_drivers_loaded()
    h = create_harness("codex", cwd=tmp_path)
    try:
        await h.start_session()
        events = [ev async for ev in h.send_turn(PROMPT)]
        text = "".join(e.text for e in events if isinstance(e, ContentDelta))
        assert "OK" in text
        assert any(e.type == "turn.completed" for e in events)
    finally:
        await h.aclose()


@pytest.mark.live
async def test_live_claude(tmp_path: Path) -> None:
    _skip_unless("claude")
    ensure_drivers_loaded()
    h = create_harness("claude", cwd=tmp_path)
    try:
        await h.start_session()
        events = [ev async for ev in h.send_turn(PROMPT)]
        text = "".join(e.text for e in events if isinstance(e, ContentDelta))
        assert "OK" in text
        assert any(e.type == "turn.completed" for e in events)
    finally:
        await h.aclose()


@pytest.mark.live
async def test_live_cursor(tmp_path: Path) -> None:
    _skip_unless("cursor-agent")
    ensure_drivers_loaded()
    h = create_harness("cursor", cwd=tmp_path)
    try:
        await h.start_session()
        events = [ev async for ev in h.send_turn(PROMPT)]
        text = "".join(e.text for e in events if isinstance(e, ContentDelta))
        assert "OK" in text
        assert any(e.type == "turn.completed" for e in events)
    finally:
        await h.aclose()


@pytest.mark.live
async def test_live_grok(tmp_path: Path) -> None:
    _skip_unless("grok", bool(os.environ.get("XAI_API_KEY")))
    ensure_drivers_loaded()
    h = create_harness("grok", cwd=tmp_path)
    try:
        await h.start_session()
        events = [ev async for ev in h.send_turn(PROMPT)]
        text = "".join(e.text for e in events if isinstance(e, ContentDelta))
        assert "OK" in text
        assert any(e.type == "turn.completed" for e in events)
    finally:
        await h.aclose()


@pytest.mark.live
async def test_live_opencode(tmp_path: Path) -> None:
    _skip_unless("opencode")
    ensure_drivers_loaded()
    h = create_harness("opencode", cwd=tmp_path)
    try:
        await h.start_session()
        events = [ev async for ev in h.send_turn(PROMPT)]
        text = "".join(e.text for e in events if isinstance(e, ContentDelta))
        assert "OK" in text
        assert any(e.type == "turn.completed" for e in events)
    finally:
        await h.aclose()
