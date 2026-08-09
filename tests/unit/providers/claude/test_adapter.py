"""Claude adapter unit tests with a fake SDK client."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import uuid4

import pytest

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.events import ToolCompletedPayload, TurnFailedPayload
from talktoharnesses.domain.models import HarnessCapabilities, HarnessConfiguration, LaunchSnapshot
from talktoharnesses.providers.adapter import StartSessionRequest, SteerRequest, TurnRequest
from talktoharnesses.providers.claude.adapter import ClaudeAdapter
from talktoharnesses.providers.claude.normalizer import ClaudeNormalizer


def _option_get(options: object, key: str) -> object | None:
    getter = getattr(options, "get", None)
    if callable(getter):
        value = getter(key)
        return value if value is not None else None
    return getattr(options, key, None)


@dataclass
class FakeClaudeClient:
    options: object
    session_id: str = ""
    prompts: list[str] = field(default_factory=list[str])
    interrupted: bool = False
    disconnected: bool = False
    responses: list[dict[str, object]] | None = None

    def __post_init__(self) -> None:
        resume = _option_get(self.options, "resume")
        session_id = _option_get(self.options, "session_id")
        if isinstance(session_id, str) and session_id:
            self.session_id = session_id
        elif isinstance(resume, str) and resume:
            self.session_id = resume
        elif not self.session_id:
            self.session_id = f"claude-{uuid4()}"

    async def connect(self, prompt: object | None = None) -> None:
        del prompt

    async def disconnect(self) -> None:
        self.disconnected = True

    async def query(self, prompt: str, session_id: str = "default") -> None:
        del session_id
        self.prompts.append(prompt)

    def receive_response(self) -> AsyncIterator[dict[str, object]]:
        async def _gen() -> AsyncIterator[dict[str, object]]:
            if self.responses is not None:
                for response in self.responses:
                    yield response
                return
            yield {
                "type": "assistant",
                "content": [{"type": "text", "text": "pong"}],
                "model": "claude",
                "session_id": self.session_id,
            }
            yield {
                "type": "result",
                "subtype": "success",
                "session_id": self.session_id,
                "is_error": False,
                "stop_reason": "end_turn",
            }

        return _gen()

    async def interrupt(self) -> None:
        self.interrupted = True


def _config() -> HarnessConfiguration:
    return HarnessConfiguration(kind=HarnessKind.CLAUDE, working_directory="/tmp")


def _launch() -> LaunchSnapshot:
    return LaunchSnapshot(
        harness_version="0.1.53+cli-2.1.88",
        working_directory="/tmp",
        adapter_version="2026.8.0.dev9",
        capabilities=HarnessCapabilities(kind=HarnessKind.CLAUDE, version="0.1.53+cli-2.1.88"),
    )


@pytest.mark.asyncio
async def test_start_submit_no_steer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_probe(config: HarnessConfiguration):
        from talktoharnesses.providers.claude.compatibility import match_release

        release = match_release(
            sdk_version="0.1.53",
            cli_version="2.1.88",
            cli_source="bundled",
            platform="linux",
        )
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("talktoharnesses.providers.claude.adapter.probe_claude", fake_probe)
    clients: list[FakeClaudeClient] = []

    def factory(options: object) -> FakeClaudeClient:
        client = FakeClaudeClient(options=options)
        clients.append(client)
        return client

    adapter = ClaudeAdapter(client_factory=factory)
    await adapter.probe(_config())
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(),
            launch=_launch(),
        )
    )
    assert session.native_session_id
    assert _option_get(clients[0].options, "session_id") == session.native_session_id
    turn_id = uuid4()
    await adapter.submit(session, TurnRequest(turn_id=turn_id, prompt="hi"))
    assert await adapter.steer(session, SteerRequest(turn_id=turn_id, prompt="more")) is False

    async def _drain() -> list[object]:
        out: list[object] = []
        async for item in adapter.events(session):
            out.append(item)
            if getattr(item, "type", None) == "turn_completed":
                return out
        return out

    events = await asyncio.wait_for(_drain(), timeout=2.0)
    assert any(getattr(e, "type", None) == "turn_completed" for e in events)
    await adapter.interrupt(session)
    assert clients[0].interrupted is True
    await adapter.close(session)
    assert clients[0].disconnected is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("responses", "error_code"),
    [
        ([], "protocol_error"),
        ([{"type": "future_message"}], "unsupported_native_event"),
    ],
)
async def test_response_protocol_error_fails_turn_and_ends_stream(
    responses: list[dict[str, object]],
    error_code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_probe(config: HarnessConfiguration):
        from talktoharnesses.providers.claude.compatibility import match_release

        release = match_release(
            sdk_version="0.1.53",
            cli_version="2.1.88",
            cli_source="bundled",
            platform="linux",
        )
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("talktoharnesses.providers.claude.adapter.probe_claude", fake_probe)

    def factory(options: object) -> FakeClaudeClient:
        return FakeClaudeClient(options=options, responses=responses)

    adapter = ClaudeAdapter(client_factory=factory)
    await adapter.probe(_config())
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(),
            launch=_launch(),
        )
    )
    turn_id = uuid4()
    await adapter.submit(session, TurnRequest(turn_id=turn_id, prompt="hi"))

    async def _drain() -> list[object]:
        return [item async for item in adapter.events(session)]

    events = await asyncio.wait_for(_drain(), timeout=2.0)
    failed = next(event for event in events if isinstance(event, TurnFailedPayload))
    assert failed.turn_id == turn_id
    assert failed.error_code == error_code
    await adapter.close(session)


def test_tool_result_uses_canonical_utf8_tail() -> None:
    normalizer = ClaudeNormalizer()
    normalizer.set_session("session-1")
    turn_id = uuid4()
    normalizer.begin_turn(turn_id)
    normalizer.on_message(
        {
            "type": "assistant",
            "content": [{"type": "tool_use", "id": "tool-1", "name": "shell", "input": {}}],
            "model": "claude",
            "session_id": "session-1",
        }
    )
    events = normalizer.on_message(
        {
            "type": "assistant",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "content": "old-output" * 300 + "é" * 1024,
                    "is_error": False,
                }
            ],
            "model": "claude",
            "session_id": "session-1",
        }
    )
    completed = next(event for event in events if isinstance(event, ToolCompletedPayload))
    assert len(completed.output_tail.encode("utf-8")) <= 2048
    assert completed.output_tail == "é" * 1024
