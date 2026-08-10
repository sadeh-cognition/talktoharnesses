"""Opt-in live Prime Agent create/resume gate.

Enable with TALKTOHARNESSES_LIVE_PRIME_AGENT=1 and
TALKTOHARNESSES_PRIME_AGENT_EXECUTABLE. When enabled, missing auth, binary, or
the exact published version is a failure rather than a skip.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from tests.live.helpers import assert_no_duplicate_first_turn, collect_turn, make_launch

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.models import HarnessConfiguration, LaunchSnapshot
from talktoharnesses.providers.adapter import ResumeSessionRequest, StartSessionRequest, TurnRequest
from talktoharnesses.providers.prime_agent import PrimeAgentAdapter
from talktoharnesses.runtime.handle import ProcessHandle
from talktoharnesses.runtime.policy import RuntimePolicy
from talktoharnesses.runtime.spec import ProcessSpec
from talktoharnesses.runtime.supervisor import ProcessSupervisor

pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_LIVE_PRIME_AGENT") != "1",
    reason="set TALKTOHARNESSES_LIVE_PRIME_AGENT=1 to run live Prime Agent tests",
)


def _executable() -> str:
    path = os.environ.get("TALKTOHARNESSES_PRIME_AGENT_EXECUTABLE")
    if not path:
        pytest.fail(
            "TALKTOHARNESSES_PRIME_AGENT_EXECUTABLE is required when live Prime Agent tests "
            "are enabled"
        )
    return path


async def _spawn(
    adapter: PrimeAgentAdapter,
    config: HarnessConfiguration,
    launch: LaunchSnapshot,
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


def _prompt(prefix: str) -> str:
    token = f"{prefix}-{uuid4().hex[:12]}"
    return f"Reply with exactly this token and do not use tools: {token}"


@pytest.mark.asyncio
async def test_live_prime_agent_create_resume(tmp_path: Path) -> None:
    executable = _executable()
    config = HarnessConfiguration(
        kind=HarnessKind.PRIME_AGENT,
        executable_path=executable,
        working_directory=str(tmp_path),
        model=os.environ.get("TALKTOHARNESSES_PRIME_AGENT_MODEL"),
    )
    first = PrimeAgentAdapter()
    caps = await first.probe(config)
    release = first._release  # pyright: ignore[reportPrivateUsage]
    assert release is not None
    assert release.id == "prime-agent-0.7.1"
    assert caps.version == "0.7.1"
    assert caps.supports_resume is True
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
        native_session_id = session.native_session_id
        assert native_session_id
        await first.submit(
            session,
            TurnRequest(turn_id=first_turn_id, prompt=_prompt("create-turn")),
        )
        first_events = await collect_turn(first, session, require_interaction=False)
    finally:
        try:
            if session is not None:
                await first.close(session)
        finally:
            await handle.close()
    assert handle.returncode is not None

    second = PrimeAgentAdapter()
    await second.probe(config)
    handle2 = await _spawn(second, config, launch)
    resumed = None
    try:
        resumed = await second.resume(
            ResumeSessionRequest(
                conversation_id=conversation_id,
                binding_id=binding_id,
                configuration=config,
                native_session_id=native_session_id,
                launch=launch,
            )
        )
        await second.submit(
            resumed,
            TurnRequest(turn_id=uuid4(), prompt=_prompt("resume-turn")),
        )
        second_events = await collect_turn(second, resumed, require_interaction=False)
        assert_no_duplicate_first_turn(
            first_events,
            second_events,
            first_turn_id=first_turn_id,
        )
        await second.interrupt(resumed)
    finally:
        try:
            if resumed is not None:
                await second.close(resumed)
        finally:
            await handle2.close()
    assert handle2.returncode is not None
    print(f"live_gate_passed release_id={release.id}")
