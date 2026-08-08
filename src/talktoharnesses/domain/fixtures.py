"""Versioned transcript fixture format for adapter contract tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

from talktoharnesses.domain._base import FROZEN
from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.events import EventPayload
from talktoharnesses.domain.models import HarnessCapabilities, LaunchSnapshot


class NativeIORecord(BaseModel):
    model_config = FROZEN

    direction: Literal["stdin", "stdout", "stderr", "http_request", "http_response", "sse"]
    payload: dict[str, Any] | str
    note: str | None = None


class ExpectedEvent(BaseModel):
    """Payload-only expectation (durable ids/sequences assigned at runtime)."""

    model_config = FROZEN

    payload: EventPayload
    sequence_index: int | None = None


class TranscriptFixture(BaseModel):
    model_config = FROZEN

    format_version: Literal[1] = 1
    harness_kind: HarnessKind
    probe: HarnessCapabilities
    launch: LaunchSnapshot
    native_io: tuple[NativeIORecord, ...] = ()
    expected_events: tuple[ExpectedEvent, ...] = ()
    redaction_assertions: tuple[str, ...] = Field(
        default=(),
        description="Substrings that must not appear in serialized fixture content.",
    )


_fixture_adapter: TypeAdapter[TranscriptFixture] = TypeAdapter(TranscriptFixture)


def load_transcript_fixture(source: Path | str | bytes | dict[str, Any]) -> TranscriptFixture:
    """Load and strictly validate a transcript fixture."""
    if isinstance(source, dict):
        return _fixture_adapter.validate_python(source)
    if isinstance(source, bytes):
        return _fixture_adapter.validate_json(source)
    if isinstance(source, Path):
        return _fixture_adapter.validate_json(source.read_bytes())
    # str: JSON text or filesystem path
    text = source.strip()
    if text.startswith("{") or text.startswith("["):
        return _fixture_adapter.validate_json(text)
    path = Path(source)
    if path.is_file():
        return _fixture_adapter.validate_json(path.read_bytes())
    return _fixture_adapter.validate_json(text)


def dump_transcript_fixture(fixture: TranscriptFixture, *, indent: int | None = 2) -> str:
    """Serialize a fixture to JSON text."""
    return fixture.model_dump_json(indent=indent)


def assert_redaction(fixture: TranscriptFixture, *texts: str) -> None:
    """Raise ValueError if any redaction assertion appears in the given texts."""
    blob = "\n".join(texts)
    for needle in fixture.redaction_assertions:
        if needle and needle in blob:
            msg = f"redaction assertion failed: {needle!r} found in content"
            raise ValueError(msg)


def fixture_to_dict(fixture: TranscriptFixture) -> dict[str, Any]:
    return json.loads(fixture.model_dump_json())
