"""OpenCode compatibility and SSE decoder tests."""

from __future__ import annotations

import pytest

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.providers.compatibility import render_supported_harnesses_markdown
from talktoharnesses.providers.opencode.argv import build_opencode_argv
from talktoharnesses.providers.opencode.compatibility import (
    load_opencode_compatibility,
    match_release,
    parse_version_stdout,
)
from talktoharnesses.providers.opencode.sse import SseDecoder


def test_load_and_match_release() -> None:
    doc = load_opencode_compatibility()
    assert doc.adapter_version == "2026.8.0.dev8"
    release = match_release("1.2.27", platform="linux")
    assert release.id == "opencode-1.2.27"


def test_unknown_version_fails() -> None:
    with pytest.raises(DomainError) as exc:
        match_release("9.9.9")
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_malformed_version_fails() -> None:
    with pytest.raises(DomainError) as exc:
        parse_version_stdout("a\nb")
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_build_argv() -> None:
    assert build_opencode_argv(port=4321) == (
        "serve",
        "--hostname",
        "127.0.0.1",
        "--port",
        "4321",
    )


def test_markdown_includes_opencode() -> None:
    md = render_supported_harnesses_markdown()
    assert "## OpenCode" in md
    assert "opencode-1.2.27" in md


def test_sse_multiline_and_comments() -> None:
    decoder = SseDecoder()
    chunk1 = b': comment\nevent: message\ndata: {"type":"server.connected"}\n'
    assert decoder.feed(chunk1) == []
    events = decoder.feed(b"\n")
    assert len(events) == 1
    assert events[0].event == "message"
    assert "server.connected" in events[0].data

    # Split across chunks
    decoder2 = SseDecoder()
    assert decoder2.feed(b"data: line1\nda") == []
    events2 = decoder2.feed(b"ta: line2\n\n")
    assert len(events2) == 1
    assert events2[0].data == "line1\nline2"
