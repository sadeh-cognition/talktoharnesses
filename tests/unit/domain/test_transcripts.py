"""Canonical transcript document dump/load and shape limits."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from talktoharnesses.domain import (
    ToolOutcome,
    dump_transcript_document,
    load_transcript_document,
)
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.transcripts import (
    TranscriptDocument,
    TranscriptMessage,
    TranscriptTool,
    TranscriptTurn,
)


def _doc(**overrides: object) -> TranscriptDocument:
    base: dict[str, object] = {
        "format": "talktoharnesses.canonical-transcript",
        "version": 1,
        "title": "Demo",
        "turns": [
            {
                "entries": [
                    {"type": "message", "role": "user", "text": "hello"},
                    {"type": "message", "role": "assistant", "text": "hi"},
                    {
                        "type": "tool",
                        "tool_name": "shell",
                        "arguments": {"cmd": "ls"},
                        "outcome": "success",
                        "output_tail": "a.txt",
                    },
                ]
            }
        ],
    }
    base.update(overrides)
    return load_transcript_document(base)


def test_dump_load_round_trip_is_deterministic() -> None:
    document = _doc()
    first = dump_transcript_document(document)
    second = dump_transcript_document(load_transcript_document(first))
    assert first == second
    assert first.endswith("\n")
    assert first == json.dumps(json.loads(first), sort_keys=True, separators=(",", ":")) + "\n"


def test_unknown_fields_and_versions_are_rejected() -> None:
    with pytest.raises(ValidationError):
        load_transcript_document(
            {
                "format": "talktoharnesses.canonical-transcript",
                "version": 1,
                "title": "x",
                "turns": [],
                "extra": True,
            }
        )
    with pytest.raises(ValidationError):
        load_transcript_document(
            {
                "format": "talktoharnesses.canonical-transcript",
                "version": 2,
                "title": "x",
                "turns": [],
            }
        )
    with pytest.raises(ValidationError):
        load_transcript_document(
            {
                "format": "other.format",
                "version": 1,
                "title": "x",
                "turns": [],
            }
        )


def test_turn_must_start_with_user_message() -> None:
    with pytest.raises(ValidationError):
        TranscriptTurn(entries=(TranscriptMessage(role="assistant", text="nope"),))
    with pytest.raises(ValidationError):
        TranscriptTurn(entries=())
    with pytest.raises(ValidationError):
        TranscriptTurn(
            entries=(
                TranscriptMessage(role="user", text="first"),
                TranscriptMessage(role="assistant", text="reply"),
                TranscriptMessage(role="user", text="second"),
            )
        )


def test_system_role_rejected() -> None:
    with pytest.raises(ValidationError):
        TranscriptMessage.model_validate({"type": "message", "role": "system", "text": "x"})


def test_entry_count_limit() -> None:
    entries = [{"type": "message", "role": "user", "text": "x"} for _ in range(5001)]
    with pytest.raises(ValidationError):
        load_transcript_document(
            {
                "format": "talktoharnesses.canonical-transcript",
                "version": 1,
                "title": "big",
                "turns": [{"entries": entries}],
            }
        )


def test_json_size_limit() -> None:
    huge = "x" * (5 * 1024 * 1024)
    with pytest.raises(DomainError) as exc:
        load_transcript_document(
            {
                "format": "talktoharnesses.canonical-transcript",
                "version": 1,
                "title": "t",
                "turns": [{"entries": [{"type": "message", "role": "user", "text": huge}]}],
            }
        )
    assert "5 MiB" in str(exc.value)


def test_tool_arguments_must_be_json() -> None:
    with pytest.raises(ValidationError):
        TranscriptTool(
            tool_name="x",
            arguments={"bad": object()},  # type: ignore[dict-item]
            outcome=ToolOutcome.SUCCESS,
        )
    with pytest.raises(ValidationError):
        TranscriptTool(
            tool_name="x",
            arguments={"bad": [float("nan"), float("inf")]},
            outcome=ToolOutcome.SUCCESS,
        )


def test_tool_output_tail_over_canonical_limit_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TranscriptTool(
            tool_name="x",
            outcome=ToolOutcome.SUCCESS,
            output_tail="é" * 1025,
        )
