"""Streaming text redactor unit tests."""

from __future__ import annotations

from talktoharnesses.application.redaction import StreamingTextRedactor


def test_basic_redaction() -> None:
    r = StreamingTextRedactor(["SECRET"])
    out = r.feed("hello SECRET world") + r.flush()
    assert "SECRET" not in out
    assert "[REDACTED]" in out


def test_split_across_chunks() -> None:
    r = StreamingTextRedactor(["SECRET"])
    a = r.feed("prefix-SECR")
    b = r.feed("ET-suffix")
    c = r.flush()
    combined = a + b + c
    assert "SECRET" not in combined
    assert "[REDACTED]" in combined
    assert "prefix-" in combined
    assert "suffix" in combined


def test_no_patterns_passthrough() -> None:
    r = StreamingTextRedactor([])
    assert r.feed("abc") == "abc"
    assert r.flush() == ""
