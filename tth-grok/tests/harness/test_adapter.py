"""Grok adapter live usage notification tests."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from uuid import uuid4

import pytest
from tth_types.adapter import ResumeSessionRequest, StartSessionRequest, TurnRequest
from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError
from tth_types.events import (
    CostUpdatedPayload,
    TurnCompletedPayload,
    UsageUpdatedPayload,
)
from tth_types.harness import HarnessConfiguration, LaunchSnapshot

from tests.fakes import _FakeAcpProcess  # pyright: ignore[reportPrivateUsage]
from tth_grok.harness.adapter import GrokAdapter
from tth_grok.harness.compatibility import match_release


class _UsageAcpProcess(_FakeAcpProcess):
    async def _respond(self, msg: dict[str, Any]) -> None:
        if msg.get("method") == "session/prompt":
            params_obj = msg.get("params")
            params = cast(dict[str, object], params_obj) if isinstance(params_obj, dict) else {}
            notification = {
                "jsonrpc": "2.0",
                "method": "_x.ai/session_notification",
                "params": {
                    "sessionId": params.get("sessionId"),
                    "update": {
                        "sessionUpdate": "turn_completed",
                        "usage": {
                            "inputTokens": 10,
                            "outputTokens": 3,
                            "totalTokens": 13,
                            "cachedReadTokens": 2,
                            "costUsdTicks": 10_000_000_000,
                        },
                    },
                },
            }
            await self._stdout_q.put(  # pyright: ignore[reportPrivateUsage]
                (json.dumps(notification) + "\n").encode()
            )
        await super()._respond(msg)


@pytest.mark.asyncio
async def test_live_xai_usage_is_emitted_before_prompt_terminal() -> None:
    release = match_release("grok 1.0.5 (5115b46bc9) [stable]", platform="linux")
    adapter = GrokAdapter()
    adapter._release = release  # pyright: ignore[reportPrivateUsage]
    adapter._capabilities = release.to_harness_capabilities()  # pyright: ignore[reportPrivateUsage]
    process = _UsageAcpProcess(agent_version="1.0.5")
    adapter.bind_process(process)  # type: ignore[arg-type]
    configuration = HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp")
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=configuration,
            launch=LaunchSnapshot(
                harness_version="1.0.5",
                working_directory="/tmp",
                adapter_version="test",
                capabilities=release.to_harness_capabilities(),
            ),
        )
    )
    turn_id = uuid4()
    await adapter.submit(session, TurnRequest(turn_id=turn_id, prompt="hello"))

    stream = adapter.events(session)
    events = [await asyncio.wait_for(anext(stream), timeout=1) for _ in range(3)]

    assert [type(event) for event in events] == [
        UsageUpdatedPayload,
        CostUpdatedPayload,
        TurnCompletedPayload,
    ]
    usage = events[0]
    assert isinstance(usage, UsageUpdatedPayload)
    assert usage.turn_id == turn_id
    assert usage.input_tokens == 10
    assert usage.output_tokens == 3
    assert usage.total_tokens == 13
    assert usage.cached_input_tokens == 2
    cost = events[1]
    assert isinstance(cost, CostUpdatedPayload)
    assert cost.turn_id == turn_id
    assert cost.cost == "1"
    assert cost.currency == "USD"
    await adapter.close(session)


# ---------------------------------------------------------------------------
# Duplicate turn submission guard
# ---------------------------------------------------------------------------


class _RecordingPromptProcess(_FakeAcpProcess):
    """Records session/prompt calls; optionally leaves them unresolved so the
    prompt watcher stays live."""

    def __init__(self, *, resolve_prompts: bool, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.prompt_calls: list[dict[str, Any]] = []
        self._resolve_prompts = resolve_prompts

    async def write_stdin(self, data: bytes) -> None:
        line = data.decode("utf-8").strip()
        if line:
            msg = json.loads(line)
            if msg.get("method") == "session/prompt":
                self.prompt_calls.append(msg)
        await super().write_stdin(data)

    async def _respond(self, msg: dict[str, Any]) -> None:
        if msg.get("method") == "session/prompt" and not self._resolve_prompts:
            return
        await super()._respond(msg)


async def _started_adapter(
    proc: _FakeAcpProcess,
    *,
    resume: bool = False,
) -> tuple[GrokAdapter, Any]:
    release = match_release("grok 1.0.5 (5115b46bc9) [stable]", platform="linux")
    adapter = GrokAdapter()
    adapter._release = release  # pyright: ignore[reportPrivateUsage]
    adapter._capabilities = release.to_harness_capabilities()  # pyright: ignore[reportPrivateUsage]
    adapter.bind_process(proc)  # type: ignore[arg-type]
    request = StartSessionRequest(
        conversation_id=uuid4(),
        binding_id=uuid4(),
        configuration=HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp"),
        launch=LaunchSnapshot(
            harness_version="1.0.5",
            working_directory="/tmp",
            adapter_version="test",
            capabilities=release.to_harness_capabilities(),
        ),
    )
    if resume:
        session = await adapter.resume(
            ResumeSessionRequest(**request.model_dump(), native_session_id="existing-session")
        )
    else:
        session = await adapter.start(request)
    return adapter, session


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize(
    ("auth_methods", "api_key", "expected_method"),
    [
        (("grok.com", "cached_token"), False, "cached_token"),
        (("cached_token", "xai.api_key"), True, "xai.api_key"),
        (("cached_token", "xai.api_key"), False, "cached_token"),
        ((), False, None),
    ],
)
async def test_authentication_precedes_session_start_and_resume(
    monkeypatch: pytest.MonkeyPatch,
    resume: bool,
    auth_methods: tuple[str, ...],
    api_key: bool,
    expected_method: str | None,
) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    if api_key:
        monkeypatch.setenv("XAI_API_KEY", "test-key")
    proc = _FakeAcpProcess(agent_version="1.0.5", auth_methods=auth_methods)
    adapter, session = await _started_adapter(proc, resume=resume)
    try:
        methods = [request["method"] for request in proc.requests]
        assert methods == [
            "initialize",
            *(["authenticate"] if expected_method else []),
            "session/load" if resume else "session/new",
        ]
        if expected_method:
            assert proc.requests[1]["params"] == {
                "methodId": expected_method,
                "_meta": {"headless": True},
            }
    finally:
        await adapter.close(session)


@pytest.mark.asyncio
async def test_duplicate_submit_same_turn_is_noop_while_prompt_active() -> None:
    proc = _RecordingPromptProcess(resolve_prompts=False, agent_version="1.0.5")
    adapter, session = await _started_adapter(proc)
    turn_id = uuid4()
    await adapter.submit(session, TurnRequest(turn_id=turn_id, prompt="one"))
    await adapter.submit(session, TurnRequest(turn_id=turn_id, prompt="one again"))
    assert len(proc.prompt_calls) == 1
    await adapter.close(session)


@pytest.mark.asyncio
async def test_submit_different_turn_while_prompt_active_raises_busy() -> None:
    proc = _RecordingPromptProcess(resolve_prompts=False, agent_version="1.0.5")
    adapter, session = await _started_adapter(proc)
    await adapter.submit(session, TurnRequest(turn_id=uuid4(), prompt="one"))
    with pytest.raises(DomainError) as exc_info:
        await adapter.submit(session, TurnRequest(turn_id=uuid4(), prompt="two"))
    assert exc_info.value.code is ErrorCode.CONVERSATION_BUSY
    assert len(proc.prompt_calls) == 1
    await adapter.close(session)


@pytest.mark.asyncio
async def test_submit_allowed_after_prompt_resolves() -> None:
    proc = _RecordingPromptProcess(resolve_prompts=True, agent_version="1.0.5")
    adapter, session = await _started_adapter(proc)
    await adapter.submit(session, TurnRequest(turn_id=uuid4(), prompt="one"))
    # The fake resolves session/prompt immediately; let the watcher finish.
    await asyncio.sleep(0.05)
    await adapter.submit(session, TurnRequest(turn_id=uuid4(), prompt="two"))
    assert len(proc.prompt_calls) == 2
    await adapter.close(session)
