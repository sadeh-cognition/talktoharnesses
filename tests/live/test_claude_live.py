"""Opt-in live Claude create/resume/interaction tests.

Enable with TALKTOHARNESSES_LIVE_CLAUDE=1.
When enabled, missing SDK/auth/CLI/version is a failure rather than a skip.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from tests.live.helpers import (
    assert_no_duplicate_first_turn,
    collect_turn,
    exercise_advertised_features,
    make_launch,
    unique_prompt,
)

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.models import HarnessConfiguration
from talktoharnesses.providers.adapter import ResumeSessionRequest, StartSessionRequest, TurnRequest
from talktoharnesses.providers.claude import ClaudeAdapter
from talktoharnesses.providers.claude.compatibility import load_claude_compatibility

pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_LIVE_CLAUDE") != "1",
    reason="set TALKTOHARNESSES_LIVE_CLAUDE=1 to run live Claude tests",
)


@pytest.mark.asyncio
async def test_live_claude_create_resume_interaction(tmp_path: Path) -> None:
    executable = os.environ.get("TALKTOHARNESSES_CLAUDE_EXECUTABLE")
    config = HarnessConfiguration(
        kind=HarnessKind.CLAUDE,
        executable_path=executable,
        working_directory=str(tmp_path),
    )
    first = ClaudeAdapter()
    caps = await first.probe(config)
    assert caps.supports_resume is True
    release = first._release  # pyright: ignore[reportPrivateUsage]
    assert release is not None
    assert release.id in {item.id for item in load_claude_compatibility().releases}
    print(f"detected_release_id={release.id}")

    conversation_id = uuid4()
    binding_id = uuid4()
    launch = make_launch(caps=caps, working_directory=str(tmp_path), resolved_executable=executable)
    first_turn_id = uuid4()
    session = await first.start(
        StartSessionRequest(
            conversation_id=conversation_id,
            binding_id=binding_id,
            configuration=config,
            launch=launch,
        )
    )
    try:
        assert session.native_session_id
        native_session_id = session.native_session_id
        await first.submit(
            session, TurnRequest(turn_id=first_turn_id, prompt=unique_prompt("create-turn"))
        )
        first_events = await collect_turn(first, session)
        seen_native, seen_offsets = first.export_seen()
    finally:
        await first.close(session)

    second = ClaudeAdapter()
    await second.probe(config)
    second.import_seen(seen_native, seen_offsets)
    resumed = await second.resume(
        ResumeSessionRequest(
            conversation_id=conversation_id,
            binding_id=binding_id,
            configuration=config,
            native_session_id=native_session_id,
            launch=launch,
        )
    )
    try:
        second_turn_id = uuid4()
        await second.submit(
            resumed, TurnRequest(turn_id=second_turn_id, prompt=unique_prompt("resume-turn"))
        )
        second_events = await collect_turn(second, resumed)
        assert_no_duplicate_first_turn(first_events, second_events, first_turn_id=first_turn_id)
        await exercise_advertised_features(second, resumed, caps)
    finally:
        await second.close(resumed)
    print(f"live_gate_passed release_id={release.id}")
