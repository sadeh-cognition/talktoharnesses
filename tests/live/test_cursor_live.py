"""Opt-in live Cursor create/resume tests.

Enable with TALKTOHARNESSES_LIVE_CURSOR=1 and TALKTOHARNESSES_CURSOR_EXECUTABLE.
When enabled, missing auth/binary/version is a failure rather than a skip.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.models import HarnessCapabilities, HarnessConfiguration, LaunchSnapshot
from talktoharnesses.providers.adapter import HarnessSession, StartSessionRequest, TurnRequest
from talktoharnesses.providers.cursor import CursorAdapter
from talktoharnesses.runtime.policy import RuntimePolicy
from talktoharnesses.runtime.spec import ProcessSpec
from talktoharnesses.runtime.supervisor import ProcessSupervisor

pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_LIVE_CURSOR") != "1",
    reason="set TALKTOHARNESSES_LIVE_CURSOR=1 to run live Cursor tests",
)


def _executable() -> str:
    path = os.environ.get("TALKTOHARNESSES_CURSOR_EXECUTABLE")
    if not path:
        pytest.fail(
            "TALKTOHARNESSES_CURSOR_EXECUTABLE is required when live Cursor tests are enabled"
        )
    return path


@pytest.mark.asyncio
async def test_live_cursor_probe_and_create(tmp_path: Path) -> None:
    executable = _executable()
    config = HarnessConfiguration(
        kind=HarnessKind.CURSOR,
        executable_path=executable,
        working_directory=str(tmp_path),
    )
    adapter = CursorAdapter()
    caps = await adapter.probe(config)
    assert isinstance(caps, HarnessCapabilities)
    assert caps.supports_resume is True

    conversation_id = uuid4()
    binding_id = uuid4()
    process_id = uuid4()
    launch = LaunchSnapshot(
        harness_version=caps.version,
        working_directory=str(tmp_path),
        adapter_version="2026.8.0.dev9",
        capabilities=caps,
        resolved_executable=executable,
    )
    supervisor = ProcessSupervisor(RuntimePolicy())
    handle = await supervisor.spawn(
        ProcessSpec(
            conversation_id=conversation_id,
            binding_id=binding_id,
            process_id=process_id,
            launch=launch,
            argv=adapter.build_argv(config),
        )
    )
    adapter.bind_process(handle)
    session: HarnessSession | None = None
    try:
        session = await adapter.start(
            StartSessionRequest(
                conversation_id=conversation_id,
                binding_id=binding_id,
                configuration=config,
                launch=launch,
            )
        )
        assert session.native_session_id
        await adapter.submit(
            session,
            TurnRequest(turn_id=uuid4(), prompt="Reply with the single word: pong"),
        )
    finally:
        if session is not None:
            await adapter.close(session)
        else:
            await handle.close()
