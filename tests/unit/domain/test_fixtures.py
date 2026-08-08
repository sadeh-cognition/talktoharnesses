"""Transcript fixture round-trips and redaction assertions."""

from __future__ import annotations

from uuid import uuid4

import pytest

from talktoharnesses.domain import (
    ExpectedEvent,
    HarnessCapabilities,
    HarnessKind,
    LaunchSnapshot,
    NativeIORecord,
    TranscriptFixture,
    assert_redaction,
    dump_transcript_fixture,
    load_transcript_fixture,
)
from talktoharnesses.domain.events import TurnStartedPayload


def _fixture() -> TranscriptFixture:
    caps = HarnessCapabilities(kind=HarnessKind.GROK, version="1.0.0")
    return TranscriptFixture(
        harness_kind=HarnessKind.GROK,
        probe=caps,
        launch=LaunchSnapshot(
            harness_version="1.0.0",
            working_directory="/tmp/ws",
            adapter_version="2026.8.0.dev1",
            capabilities=caps,
        ),
        native_io=(NativeIORecord(direction="stdout", payload={"method": "session/update"}),),
        expected_events=(
            ExpectedEvent(payload=TurnStartedPayload(turn_id=uuid4()), sequence_index=0),
        ),
        redaction_assertions=("SECRET_TOKEN",),
    )


def test_fixture_json_round_trip() -> None:
    fixture = _fixture()
    raw = dump_transcript_fixture(fixture)
    loaded = load_transcript_fixture(raw)
    assert loaded.format_version == 1
    assert loaded.harness_kind is HarnessKind.GROK
    assert loaded.probe.version == "1.0.0"
    assert len(loaded.expected_events) == 1
    assert loaded.expected_events[0].payload.type == "turn_started"


def test_redaction_assertions() -> None:
    fixture = _fixture()
    assert_redaction(fixture, "clean content")
    with pytest.raises(ValueError, match="SECRET_TOKEN"):
        assert_redaction(fixture, "leaked SECRET_TOKEN value")
