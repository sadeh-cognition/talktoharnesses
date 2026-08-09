"""Converters between retained handoff entries and canonical transcripts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast
from uuid import uuid4

from talktoharnesses.application.handoff import (
    HandoffDocument,
    HandoffMessage,
    HandoffTool,
    handoff_sort_key,
)
from talktoharnesses.application.redaction import StreamingTextRedactor
from talktoharnesses.domain.enums import MessageRole
from talktoharnesses.domain.transcripts import (
    TranscriptDocument,
    TranscriptMessage,
    TranscriptTool,
    TranscriptTurn,
)


def handoff_to_transcript(handoff: HandoffDocument, title: str) -> TranscriptDocument:
    """Group ordered handoff entries by ``turn_id`` into transcript turns."""
    ordered = sorted(handoff.entries, key=handoff_sort_key)
    turns: list[TranscriptTurn] = []
    current_turn_id = None
    current_entries: list[TranscriptMessage | TranscriptTool] = []

    def flush() -> None:
        nonlocal current_entries
        if current_entries:
            turns.append(TranscriptTurn(entries=tuple(current_entries)))
            current_entries = []

    for entry in ordered:
        if current_turn_id is None:
            current_turn_id = entry.turn_id
        elif entry.turn_id != current_turn_id:
            flush()
            current_turn_id = entry.turn_id
        if isinstance(entry, HandoffMessage):
            if entry.role is MessageRole.SYSTEM:
                continue
            current_entries.append(
                TranscriptMessage(
                    role="user" if entry.role is MessageRole.USER else "assistant",
                    text=entry.text,
                    interrupted=entry.interrupted,
                )
            )
        else:
            current_entries.append(
                TranscriptTool(
                    tool_name=entry.tool_name,
                    arguments=dict(entry.arguments),
                    outcome=entry.outcome,
                    exit_status=entry.exit_status,
                    paths=entry.paths,
                    output_tail=entry.output_tail,
                )
            )
    flush()
    return TranscriptDocument(
        format="talktoharnesses.canonical-transcript",
        version=1,
        title=title,
        turns=tuple(turns),
    )


def transcript_to_handoff(document: TranscriptDocument) -> HandoffDocument:
    """Allocate prospective local UUIDs and flatten turns into a handoff document."""
    entries: list[HandoffMessage | HandoffTool] = []
    for turn_order_index, turn in enumerate(document.turns, start=1):
        turn_id = uuid4()
        for order_index, entry in enumerate(turn.entries, start=1):
            if isinstance(entry, TranscriptMessage):
                entries.append(
                    HandoffMessage(
                        id=uuid4(),
                        turn_id=turn_id,
                        role=MessageRole.USER if entry.role == "user" else MessageRole.ASSISTANT,
                        text=entry.text,
                        interrupted=entry.interrupted,
                        turn_order_index=turn_order_index,
                        order_index=order_index,
                    )
                )
            else:
                entries.append(
                    HandoffTool(
                        id=uuid4(),
                        turn_id=turn_id,
                        tool_name=entry.tool_name,
                        arguments=dict(entry.arguments),
                        outcome=entry.outcome,
                        exit_status=entry.exit_status,
                        paths=entry.paths,
                        output_tail=entry.output_tail,
                        turn_order_index=turn_order_index,
                        order_index=order_index,
                    )
                )
    return HandoffDocument(entries=tuple(entries))


def redact_transcript(
    document: TranscriptDocument,
    patterns: Sequence[str],
) -> TranscriptDocument:
    """Apply the streaming redactor to owner-visible transcript text fields."""
    if not patterns:
        return document

    def _redact(text: str) -> str:
        redactor = StreamingTextRedactor(patterns)
        return redactor.feed(text) + redactor.flush()

    def _redact_json(value: object) -> object:
        if isinstance(value, str):
            return _redact(value)
        if isinstance(value, dict):
            mapping = cast(dict[str, object], value)
            return {_redact(key): _redact_json(item) for key, item in mapping.items()}
        if isinstance(value, list):
            return [_redact_json(item) for item in cast(list[object], value)]
        return value

    turns: list[TranscriptTurn] = []
    for turn in document.turns:
        entries: list[TranscriptMessage | TranscriptTool] = []
        for entry in turn.entries:
            if isinstance(entry, TranscriptMessage):
                entries.append(entry.model_copy(update={"text": _redact(entry.text)}))
            else:
                entries.append(
                    entry.model_copy(
                        update={
                            "tool_name": _redact(entry.tool_name),
                            "arguments": _redact_json(entry.arguments),
                            "paths": tuple(_redact(path) for path in entry.paths),
                            "output_tail": _redact(entry.output_tail),
                        }
                    )
                )
        turns.append(TranscriptTurn(entries=tuple(entries)))
    return document.model_copy(
        update={
            "title": _redact(document.title),
            "turns": tuple(turns),
        }
    )
