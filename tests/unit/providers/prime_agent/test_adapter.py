"""Prime Agent RPC adapter behavior."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import HarnessCapabilities, HarnessConfiguration, LaunchSnapshot
from talktoharnesses.providers.adapter import (
    ResumeSessionRequest,
    StartSessionRequest,
    SteerRequest,
    TurnRequest,
)
from talktoharnesses.providers.prime_agent.adapter import PrimeAgentAdapter


class _FakePrimeProcess:
    def __init__(self, session_file: str | None = None) -> None:
        self._stdout: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.session_file = session_file or f"/tmp/{uuid4()}.jsonl"
        self.session_id = self.session_file.rsplit("/", 1)[-1].removesuffix(".jsonl")
        self.commands: list[dict[str, Any]] = []
        self.closed = False

    async def write_stdin(self, data: bytes) -> None:
        command = json.loads(data)
        self.commands.append(command)
        command_type = command["type"]
        if command_type == "switch_session":
            self.session_file = command["sessionPath"]
            self.session_id = self.session_file.rsplit("/", 1)[-1].removesuffix(".jsonl")
        response: dict[str, Any] = {
            "id": command["id"],
            "type": "response",
            "command": command_type,
            "success": True,
        }
        if command_type == "get_state":
            response["data"] = {
                "sessionFile": self.session_file,
                "sessionId": self.session_id,
            }
        await self._emit(response)
        if command_type == "prompt":
            await self._emit(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "done"},
                }
            )
            await self._emit({"type": "message_end", "message": {"role": "assistant"}})
            await self._emit({"type": "agent_end", "messages": []})

    def stdout(self) -> AsyncIterator[bytes]:
        async def _stream() -> AsyncIterator[bytes]:
            while True:
                item = await self._stdout.get()
                if item is None:
                    return
                yield item

        return _stream()

    async def close_stdin(self) -> None:
        self.closed = True
        await self._stdout.put(None)

    async def _emit(self, value: dict[str, Any]) -> None:
        await self._stdout.put((json.dumps(value) + "\n").encode())


def _config() -> HarnessConfiguration:
    return HarnessConfiguration(
        kind=HarnessKind.PRIME_AGENT,
        executable_path="/bin/true",
        working_directory="/tmp",
        model="anthropic/claude-sonnet-4-5",
        effort="high",
    )


def _launch() -> LaunchSnapshot:
    capabilities = HarnessCapabilities(
        kind=HarnessKind.PRIME_AGENT,
        version="0.7.1",
        supports_resume=True,
        supports_steer=True,
    )
    return LaunchSnapshot(
        resolved_executable="/bin/true",
        harness_version="0.7.1",
        working_directory="/tmp",
        adapter_version="2026.8.1",
        capabilities=capabilities,
    )


def test_build_argv_uses_effort_and_rejects_legacy_mode() -> None:
    adapter = PrimeAgentAdapter()
    assert adapter.build_argv(_config())[-2:] == ("--thinking", "high")
    with pytest.raises(DomainError, match="mode no longer represents thinking"):
        adapter.build_argv(_config().model_copy(update={"mode": "high", "effort": None}))


async def _probe(config: HarnessConfiguration):
    del config
    from talktoharnesses.providers.prime_agent.compatibility import match_release

    release = match_release("0.7.1", platform="linux")
    return release.to_harness_capabilities(), release


@pytest.mark.asyncio
async def test_start_submit_steer_and_close(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "talktoharnesses.providers.prime_agent.adapter.probe_prime_agent",
        _probe,
    )
    process = _FakePrimeProcess()
    adapter = PrimeAgentAdapter()
    adapter.bind_process(process)  # type: ignore[arg-type]
    await adapter.probe(_config())
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(),
            launch=_launch(),
        )
    )
    assert session.native_session_id == process.session_file
    turn_id = uuid4()
    await adapter.submit(session, TurnRequest(turn_id=turn_id, prompt="finish"))
    assert await adapter.steer(session, SteerRequest(turn_id=turn_id, prompt="more")) is False

    event_types: list[str] = []
    async for event in adapter.events(session):
        event_type = getattr(event, "type", None)
        assert isinstance(event_type, str)
        event_types.append(event_type)
        if event_type == "turn_completed":
            break
    assert event_types == [
        "assistant_message_started",
        "assistant_message_delta",
        "assistant_message_completed",
        "turn_completed",
    ]
    await adapter.close(session)
    assert process.closed


@pytest.mark.asyncio
async def test_resume_switches_to_persisted_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "talktoharnesses.providers.prime_agent.adapter.probe_prime_agent",
        _probe,
    )
    original = "/tmp/existing-prime-session.jsonl"
    process = _FakePrimeProcess()
    adapter = PrimeAgentAdapter()
    adapter.bind_process(process)  # type: ignore[arg-type]
    await adapter.probe(_config())
    session = await adapter.resume(
        ResumeSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(),
            native_session_id=original,
            launch=_launch(),
        )
    )
    assert session.native_session_id == original
    assert process.commands[0]["type"] == "switch_session"
    assert process.commands[0]["sessionPath"] == original
    await adapter.close(session)


@pytest.mark.asyncio
async def test_yolo_is_accepted_on_create_and_resume_without_argv_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "talktoharnesses.providers.prime_agent.adapter.probe_prime_agent",
        _probe,
    )
    yolo = HarnessConfiguration(
        kind=HarnessKind.PRIME_AGENT,
        executable_path="/bin/true",
        working_directory="/tmp",
        model="anthropic/claude-sonnet-4-5",
        effort="high",
        yolo=True,
    )
    process = _FakePrimeProcess()
    adapter = PrimeAgentAdapter()
    adapter.bind_process(process)  # type: ignore[arg-type]
    await adapter.probe(yolo)
    assert adapter.build_argv(yolo) == adapter.build_argv(_config())
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=yolo,
            launch=_launch(),
        )
    )
    await adapter.close(session)

    resume_process = _FakePrimeProcess()
    resume_adapter = PrimeAgentAdapter()
    resume_adapter.bind_process(resume_process)  # type: ignore[arg-type]
    await resume_adapter.probe(yolo)
    resumed = await resume_adapter.resume(
        ResumeSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=yolo,
            native_session_id="/tmp/existing-prime-session.jsonl",
            launch=_launch(),
        )
    )
    assert resume_process.commands[0]["type"] == "switch_session"
    await resume_adapter.close(resumed)


@pytest.mark.asyncio
async def test_turn_model_override_is_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "talktoharnesses.providers.prime_agent.adapter.probe_prime_agent",
        _probe,
    )
    process = _FakePrimeProcess()
    adapter = PrimeAgentAdapter()
    adapter.bind_process(process)  # type: ignore[arg-type]
    await adapter.probe(_config())
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(),
            launch=_launch(),
        )
    )

    async def submit_and_drain(model: str | None) -> None:
        await adapter.submit(
            session,
            TurnRequest(turn_id=uuid4(), prompt="finish", model=model),
        )
        async for event in adapter.events(session):
            if event.type == "turn_completed":
                return

    await submit_and_drain("openai/gpt-5.2")
    await submit_and_drain(None)

    model_commands = [item for item in process.commands if item["type"] == "set_model"]
    assert [(item["provider"], item["modelId"]) for item in model_commands] == [
        ("openai", "gpt-5.2"),
        ("anthropic", "claude-sonnet-4-5"),
    ]
    await adapter.close(session)
