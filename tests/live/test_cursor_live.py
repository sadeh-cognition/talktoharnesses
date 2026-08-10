"""Opt-in live Cursor create/resume/interaction tests.

Enable with TALKTOHARNESSES_LIVE_CURSOR=1 and TALKTOHARNESSES_CURSOR_EXECUTABLE.
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
from talktoharnesses.providers.cursor import CursorAdapter
from talktoharnesses.providers.cursor.compatibility import load_cursor_compatibility
from talktoharnesses.runtime.handle import ProcessHandle
from talktoharnesses.runtime.policy import RuntimePolicy
from talktoharnesses.runtime.spec import ProcessSpec
from talktoharnesses.runtime.supervisor import ProcessSupervisor

pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_LIVE_CURSOR") != "1",
    reason="set TALKTOHARNESSES_LIVE_CURSOR=1 to run live Cursor tests",
)

_PINNED_RELEASE = "cursor-2026.08.04-aaa8809"
_MODEL = "composer-2.5[fast=false]"
_MODE = "ask"


def _executable() -> str:
    path = os.environ.get("TALKTOHARNESSES_CURSOR_EXECUTABLE")
    if not path:
        pytest.fail(
            "TALKTOHARNESSES_CURSOR_EXECUTABLE is required when live Cursor tests are enabled"
        )
    return path


def _config(executable: str, working_directory: str) -> HarnessConfiguration:
    return HarnessConfiguration(
        kind=HarnessKind.CURSOR,
        executable_path=executable,
        working_directory=working_directory,
        model=_MODEL,
        mode=_MODE,
    )


async def _spawn(
    adapter: CursorAdapter, config: HarnessConfiguration, launch: LaunchSnapshot
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
async def test_live_cursor_create_resume_interaction(tmp_path: Path) -> None:
    executable = _executable()
    config = _config(executable, str(tmp_path))
    first = CursorAdapter()
    caps = await first.probe(config)
    assert caps.supports_resume is True
    release = first._release  # pyright: ignore[reportPrivateUsage]
    assert release is not None
    assert release.id == _PINNED_RELEASE
    assert release.id in {item.id for item in load_cursor_compatibility().releases}
    print(f"detected_release_id={release.id}")

    conversation_id = uuid4()
    binding_id = uuid4()
    launch = make_launch(
        caps=caps,
        working_directory=str(tmp_path),
        resolved_executable=executable,
    )
    # Align launch metadata with configuration used for ACP selection.
    launch = launch.model_copy(update={"model": _MODEL, "mode": _MODE})
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
        assert session.model == _MODEL
        assert session.mode == _MODE
        # Successful setters leave currentValue matching the request.
        current = first._current_model_selection  # pyright: ignore[reportPrivateUsage]
        baseline = first._session_model_selection  # pyright: ignore[reportPrivateUsage]
        assert current is not None and baseline is not None
        assert current.model_id == "composer-2.5"
        assert ("fast", "false") in current.parameters
        assert current == baseline

        await first.submit(
            session, TurnRequest(turn_id=first_turn_id, prompt=unique_prompt("create-turn"))
        )
        first_events = await collect_turn(first, session)

        # One-turn override to fast=true, then restore on the next unoverridden turn.
        override_turn = uuid4()
        await first.submit(
            session,
            TurnRequest(
                turn_id=override_turn,
                prompt=unique_prompt("override-turn"),
                model="composer-2.5[fast=true]",
            ),
        )
        await collect_turn(first, session)
        overridden = first._current_model_selection  # pyright: ignore[reportPrivateUsage]
        assert overridden is not None
        assert ("fast", "true") in overridden.parameters
        assert first._session_model_selection == baseline  # pyright: ignore[reportPrivateUsage]

        restore_turn = uuid4()
        await first.submit(
            session,
            TurnRequest(turn_id=restore_turn, prompt=unique_prompt("restore-turn")),
        )
        await collect_turn(first, session)
        restored = first._current_model_selection  # pyright: ignore[reportPrivateUsage]
        assert restored == baseline
        assert ("fast", "false") in (restored.parameters if restored else ())

        seen_native, seen_offsets = first.export_seen()
    finally:
        try:
            if session is not None:
                await first.close(session)
        finally:
            await handle.close()
    assert handle.returncode is not None

    second = CursorAdapter()
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
        resumed_selection = second._current_model_selection  # pyright: ignore[reportPrivateUsage]
        assert resumed_selection is not None
        assert resumed_selection.model_id == "composer-2.5"
        assert ("fast", "false") in resumed_selection.parameters

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
