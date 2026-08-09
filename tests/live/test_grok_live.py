"""Opt-in live Grok create/resume/interaction tests.

Enable with TALKTOHARNESSES_LIVE_GROK=1 and TALKTOHARNESSES_GROK_EXECUTABLE.
When enabled, missing auth/binary/version is a failure rather than a skip.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from tests.live.helpers import (
    assert_no_duplicate_first_turn,
    collect_turn,
    make_launch,
    unique_prompt,
)

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.models import HarnessConfiguration, LaunchSnapshot
from talktoharnesses.providers.adapter import ResumeSessionRequest, StartSessionRequest, TurnRequest
from talktoharnesses.providers.grok import GrokAdapter
from talktoharnesses.providers.grok.compatibility import load_grok_compatibility
from talktoharnesses.runtime.handle import ProcessHandle
from talktoharnesses.runtime.policy import RuntimePolicy
from talktoharnesses.runtime.spec import ProcessSpec
from talktoharnesses.runtime.supervisor import ProcessSupervisor

pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_LIVE_GROK") != "1",
    reason="set TALKTOHARNESSES_LIVE_GROK=1 to run live Grok tests",
)


def _executable() -> str:
    path = os.environ.get("TALKTOHARNESSES_GROK_EXECUTABLE")
    if not path:
        pytest.fail("TALKTOHARNESSES_GROK_EXECUTABLE is required when live Grok tests are enabled")
    return path


async def _spawn(
    adapter: GrokAdapter, config: HarnessConfiguration, launch: LaunchSnapshot
) -> ProcessHandle:
    supervisor = ProcessSupervisor(RuntimePolicy())
    handle = await supervisor.spawn(
        ProcessSpec(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            process_id=uuid4(),
            launch=launch,
            argv=adapter.build_argv(config),
        )
    )
    adapter.bind_process(handle)
    return handle


@pytest.mark.asyncio
async def test_live_grok_create_resume_interaction(tmp_path: Path) -> None:
    executable = _executable()
    config = HarnessConfiguration(
        kind=HarnessKind.GROK,
        executable_path=executable,
        working_directory=str(tmp_path),
    )
    first = GrokAdapter()
    caps = await first.probe(config)
    assert caps.supports_resume is True
    release = first._release  # pyright: ignore[reportPrivateUsage]
    assert release is not None
    assert release.id in {item.id for item in load_grok_compatibility().releases}
    print(f"detected_release_id={release.id}")

    conversation_id = uuid4()
    binding_id = uuid4()
    launch = make_launch(caps=caps, working_directory=str(tmp_path), resolved_executable=executable)
    handle = await _spawn(first, config, launch)
    first_turn_id = uuid4()
    session = None
    try:
        session = await first.start(
            StartSessionRequest(
                conversation_id=conversation_id,
                binding_id=binding_id,
                configuration=config,
                launch=launch,
            )
        )
        assert session.native_session_id
        native_session_id = session.native_session_id
        await first.submit(
            session, TurnRequest(turn_id=first_turn_id, prompt=unique_prompt("create-turn"))
        )
        first_events = await collect_turn(first, session)
        seen_native, seen_offsets = first.export_seen()
    finally:
        try:
            if session is not None:
                await first.close(session)
        finally:
            await handle.close()
    assert handle.returncode is not None

    second = GrokAdapter()
    await second.probe(config)
    handle2 = await _spawn(second, config, launch)
    resumed = None
    try:
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
        second_turn_id = uuid4()
        await second.submit(
            resumed, TurnRequest(turn_id=second_turn_id, prompt=unique_prompt("resume-turn"))
        )
        second_events = await collect_turn(second, resumed)
        assert_no_duplicate_first_turn(first_events, second_events, first_turn_id=first_turn_id)
        await second.interrupt(resumed)
    finally:
        try:
            if resumed is not None:
                await second.close(resumed)
        finally:
            await handle2.close()
    assert handle2.returncode is not None
    print(f"live_gate_passed release_id={release.id}")
