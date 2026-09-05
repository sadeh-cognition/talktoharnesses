"""Resume cleanup, null-tolerant MSP parsing, and probe spawn caching."""

from __future__ import annotations

import asyncio
import sys
from typing import Any, cast
from uuid import uuid4

import pytest
from tests.fakes import FLOOR_VERSION, FakeMuseHost, capabilities_for, config_for, patch_probe
from tth_types.adapter import (
    HarnessInteractionRequest,
    ResumeSessionRequest,
    StartSessionRequest,
    TurnRequest,
)
from tth_types.enums import HarnessKind
from tth_types.harness import HarnessConfiguration, HarnessModelInfo, LaunchSnapshot

from tth_muse.harness import probe as probe_module
from tth_muse.harness.adapter import MuseAdapter
from tth_muse.harness.normalizer import MuseNormalizer
from tth_muse.runtime.handle import ProcessHandle


async def _adapter(
    monkeypatch: pytest.MonkeyPatch, host: FakeMuseHost, *, resume: bool = False
) -> tuple[MuseAdapter, Any]:
    patch_probe(monkeypatch)
    adapter = MuseAdapter()
    config = config_for(HarnessKind.MUSE)
    await adapter.probe(config)
    adapter.bind_process(cast(ProcessHandle, cast(object, host)))
    caps = capabilities_for(HarnessKind.MUSE)
    request = StartSessionRequest(
        conversation_id=uuid4(),
        binding_id=uuid4(),
        configuration=config,
        launch=LaunchSnapshot(
            harness_version=caps.version,
            working_directory="/tmp",
            capabilities=caps,
            adapter_version="test",
        ),
    )
    if resume:
        session = await adapter.resume(
            ResumeSessionRequest(**request.model_dump(), native_session_id=host.session_id)
        )
    else:
        session = await adapter.start(request)
    return adapter, session


async def test_resume_interrupts_native_turns_left_waiting_on_orphaned_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proxy terminalized the old turn before resuming, so host-side pending
    requests can never be answered: release them instead of dropping them."""
    host = FakeMuseHost()
    host.pending = {
        "approvals": [
            {"approvalId": "a1", "turnId": "turn-old", "toolName": "write_file"},
            {"approvalId": "a2", "turnId": "turn-old", "toolName": "bash"},
        ],
        "userInputs": [{"userInputId": "u1", "turnId": "turn-older", "questions": []}],
    }
    adapter, session = await _adapter(monkeypatch, host, resume=True)
    try:
        interrupts = [
            frame["params"] for frame in host.commands if frame["method"] == "turn/interrupt"
        ]
        assert sorted(row["turnId"] for row in interrupts) == ["turn-old", "turn-older"]
        assert all(row["sessionId"] == host.session_id for row in interrupts)
        # Nothing was surfaced as a TTH interaction: there is no turn to own it.
        assert adapter._queue.empty()  # pyright: ignore[reportPrivateUsage]
        assert adapter._pending == {}  # pyright: ignore[reportPrivateUsage]
    finally:
        await adapter.close(session)


async def test_resume_with_null_pending_lists_is_tolerated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = FakeMuseHost()
    host.pending = cast(dict[str, list[dict[str, Any]]], {"approvals": None, "userInputs": None})
    adapter, session = await _adapter(monkeypatch, host, resume=True)
    try:
        assert not any(frame["method"] == "turn/interrupt" for frame in host.commands)
    finally:
        await adapter.close(session)


async def test_null_optionals_in_requests_do_not_tear_down_the_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = FakeMuseHost()
    host.complete_turns = False
    adapter, session = await _adapter(monkeypatch, host)
    try:
        await adapter.submit(session, TurnRequest(turn_id=uuid4(), prompt="ask"))
        native_turn = next(
            frame["params"]["commandId"]
            for frame in host.commands
            if frame["method"] == "turn/start"
        )
        await host.emit(
            "approval/requested",
            sessionId=host.session_id,
            turnId=native_turn,
            approvalId="a1",
            toolName="bash",
            rawArgs=None,
            currentRequirementId=None,
            availableChoices=[{"choiceId": "allow", "decision": "approved"}],
        )
        await host.emit(
            "userInput/requested",
            sessionId=host.session_id,
            turnId=native_turn,
            userInputId="u1",
            questions=[
                {
                    "id": "q1",
                    "prompt": "Pick one",
                    "options": [{"label": "A", "value": "a"}],
                    "selection": None,
                }
            ],
        )
        first = await asyncio.wait_for(anext(adapter.events(session)), 1)
        second = await asyncio.wait_for(anext(adapter.events(session)), 1)
        assert isinstance(first, HarnessInteractionRequest)
        assert isinstance(second, HarnessInteractionRequest)
        # The reader is still alive: a follow-up request round-trips.
        connection = adapter._connection  # pyright: ignore[reportPrivateUsage]
        assert connection is not None
        assert await connection.request("model/list")
    finally:
        await adapter.close(session)


async def test_notification_handler_failure_does_not_close_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = FakeMuseHost()
    host.complete_turns = False
    adapter, session = await _adapter(monkeypatch, host)
    try:
        await adapter.submit(session, TurnRequest(turn_id=uuid4(), prompt="ask"))
        # approvalId is mandatory; its absence raises inside the handler.
        await host.emit("approval/requested", sessionId=host.session_id, turnId="x")
        connection = adapter._connection  # pyright: ignore[reportPrivateUsage]
        assert connection is not None
        assert await connection.request("model/list")
        assert adapter._queue.empty()  # pyright: ignore[reportPrivateUsage]
    finally:
        await adapter.close(session)


@pytest.mark.parametrize(
    "params",
    [
        {"sessionId": "session", "item": None},
        {"sessionId": "session", "item": {"kind": "toolCall", "itemId": None}},
        {"sessionId": "session", "usage": None, "promptTokens": 3},
        {"sessionId": "session", "terminal": "failed", "error": None},
    ],
)
def test_normalizer_tolerates_null_optionals(params: dict[str, Any]) -> None:
    normalizer = MuseNormalizer()
    normalizer.session_id = "session"
    normalizer.begin_turn(uuid4())
    method = (
        "turn/completed"
        if "terminal" in params
        else "session/tokenUsage"
        if "promptTokens" in params
        else "item/started"
    )
    events = normalizer.on_notification(method, params)
    if method == "turn/completed":
        assert events[-1].model_dump(mode="json")["message"] == "Muse turn failed"
    elif method == "session/tokenUsage":
        assert events[-1].model_dump(mode="json")["input_tokens"] == 3
    else:
        assert events == []


def test_tool_items_use_the_official_tool_field() -> None:
    normalizer = MuseNormalizer()
    normalizer.session_id = "session"
    normalizer.begin_turn(uuid4())
    events = normalizer.on_notification(
        "item/started",
        {
            "sessionId": "session",
            "item": {"itemId": "i1", "kind": "toolCall", "tool": "write_file", "args": None},
        },
    )
    assert [event.model_dump(mode="json")["tool_name"] for event in events] == [
        "write_file",
        "write_file",
    ]


async def test_probe_caches_host_inspection_per_executable_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TALKTOHARNESSES_MUSE_EXECUTABLE", sys.executable)
    probe_module.reset_probe_cache_for_tests()
    calls = 0

    async def inspect(
        _executable: Any, _config: HarnessConfiguration
    ) -> tuple[str, tuple[HarnessModelInfo, ...]]:
        nonlocal calls
        calls += 1
        return FLOOR_VERSION, (HarnessModelInfo(id="default", label="Default"),)

    monkeypatch.setattr(probe_module, "_inspect_host", inspect)
    config = HarnessConfiguration(kind=HarnessKind.MUSE, working_directory="/tmp")
    try:
        first = await probe_module.probe_muse(config)
        second = await probe_module.probe_muse(config.model_copy(update={"model": "default"}))
        assert calls == 1
        assert first.version == second.version == FLOOR_VERSION
        assert [model.id for model in second.models] == ["default"]
        with pytest.raises(Exception, match="not in its catalog"):
            await probe_module.probe_muse(config.model_copy(update={"model": "missing"}))
        assert calls == 1
        probe_module.reset_probe_cache_for_tests()
        await probe_module.probe_muse(config)
        assert calls == 2
    finally:
        probe_module.reset_probe_cache_for_tests()
