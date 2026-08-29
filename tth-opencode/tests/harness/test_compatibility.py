"""OpenCode compatibility and SSE decoder tests."""

from __future__ import annotations

import pytest
from tth_types.enums import ErrorCode
from tth_types.errors import DomainError

from tth_opencode.harness.argv import build_opencode_argv
from tth_opencode.harness.compatibility import (
    load_opencode_compatibility,
    match_release,
    parse_version_stdout,
)
from tth_opencode.shared.sse_decoder import SseDecoder


def test_load_and_match_release() -> None:
    doc = load_opencode_compatibility()
    assert doc.adapter_version == "2026.8.5"
    release = match_release("1.2.27", platform="linux")
    assert release.id == "opencode-1.2.27"


def test_newer_patch_above_floor_is_accepted() -> None:
    release = match_release("1.2.30", platform="linux")
    assert release.id == "opencode-1.2.30"


def test_below_floor_fails() -> None:
    with pytest.raises(DomainError) as exc:
        match_release("1.2.0")
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
