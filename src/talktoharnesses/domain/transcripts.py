"""Versioned provider-neutral canonical transcript document (Phase 11).

Export/import use this shape only. It carries retained user/assistant messages
and canonical tool results — never reasoning, plans, raw events, native IDs,
or workspace files. See ``docs/phase11.md``.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator

from talktoharnesses.domain._base import FROZEN
from talktoharnesses.domain.enums import ErrorCode, ToolOutcome
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import limit_tool_output_tail

# Match CanonicalToolResult / Django CharField limits.
_MAX_TITLE_CHARS = 512
_MAX_TOOL_NAME_CHARS = 255
_MAX_ENTRIES = 5000
_MAX_JSON_BYTES = 5 * 1024 * 1024

# Same recursive JSON surface as CanonicalToolResult.arguments (dict[str, Any]).
JsonValue = Any

_TRANSCRIPT_FORMAT = "talktoharnesses.canonical-transcript"


class TranscriptMessage(BaseModel):
    model_config = FROZEN

    type: Literal["message"] = "message"
    role: Literal["user", "assistant"]
    text: str
    interrupted: bool = False


class TranscriptTool(BaseModel):
    model_config = FROZEN

    type: Literal["tool"] = "tool"
    tool_name: str = Field(max_length=_MAX_TOOL_NAME_CHARS)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    outcome: ToolOutcome
    exit_status: int | None = None
    paths: tuple[str, ...] = ()
    output_tail: str = ""

    @field_validator("arguments")
    @classmethod
    def _json_arguments(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        # Reject non-JSON-serializable values early (matches wire contract).
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("tool arguments must be JSON-serializable") from exc
        return value

    @field_validator("output_tail")
    @classmethod
    def _limit_tail(cls, value: str) -> str:
        # Reuse the canonical UTF-8-safe 2 KiB boundary without silently
        # changing an imported document.
        if limit_tool_output_tail(value) != value:
            raise ValueError("tool output_tail exceeds 2 KiB")
        return value


TranscriptEntry = Annotated[
    TranscriptMessage | TranscriptTool,
    Field(discriminator="type"),
]


class TranscriptTurn(BaseModel):
    model_config = FROZEN

    entries: tuple[TranscriptEntry, ...]

    @model_validator(mode="after")
    def _require_leading_user_message(self) -> TranscriptTurn:
        if not self.entries:
            raise ValueError("turn must contain at least one entry")
        first = self.entries[0]
        if not isinstance(first, TranscriptMessage) or first.role != "user":
            raise ValueError("each turn must start with a user message")
        if any(
            isinstance(entry, TranscriptMessage) and entry.role == "user"
            for entry in self.entries[1:]
        ):
            raise ValueError("each turn must contain exactly one user message")
        return self


class TranscriptDocument(BaseModel):
    model_config = FROZEN

    format: Literal["talktoharnesses.canonical-transcript"]
    version: Literal[1]
    title: str = Field(max_length=_MAX_TITLE_CHARS)
    turns: tuple[TranscriptTurn, ...]

    @model_validator(mode="after")
    def _entry_count_limit(self) -> TranscriptDocument:
        total = sum(len(turn.entries) for turn in self.turns)
        if total > _MAX_ENTRIES:
            raise ValueError(f"transcript exceeds {_MAX_ENTRIES} entries")
        return self


_document_adapter: TypeAdapter[TranscriptDocument] = TypeAdapter(TranscriptDocument)


def dump_transcript_document(document: TranscriptDocument) -> str:
    """Serialize to deterministic canonical JSON with one trailing newline."""
    payload = document.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"


def load_transcript_document(source: str | bytes | dict[str, Any]) -> TranscriptDocument:
    """Strictly validate a transcript document and enforce size limits."""
    if isinstance(source, dict):
        # JSON round-trip so list→tuple / enum coercion matches the wire form.
        raw = json.dumps(source, ensure_ascii=False).encode("utf-8")
    elif isinstance(source, str):
        raw = source.encode("utf-8")
    else:
        raw = source

    if len(raw) > _MAX_JSON_BYTES:
        raise DomainError(
            ErrorCode.INVALID_STATE,
            "transcript JSON exceeds 5 MiB",
        )
    document = _document_adapter.validate_json(raw)
    # Re-check via canonical form so whitespace inflation cannot bypass limits.
    encoded = dump_transcript_document(document).encode("utf-8")
    if len(encoded) > _MAX_JSON_BYTES:
        raise DomainError(
            ErrorCode.INVALID_STATE,
            "transcript JSON exceeds 5 MiB",
        )
    if document.format != _TRANSCRIPT_FORMAT:
        raise DomainError(ErrorCode.INVALID_STATE, "unsupported transcript format")
    if document.version != 1:
        raise DomainError(ErrorCode.INVALID_STATE, "unsupported transcript version")
    return document
