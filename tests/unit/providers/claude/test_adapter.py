"""Claude adapter unit tests with a fake SDK client."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
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
        adapter_version="2026.8.1",
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


@pytest.mark.asyncio
async def test_can_use_tool_and_answer_interaction() -> None:
    from talktoharnesses.domain.enums import ApprovalDecision, ErrorCode
    from talktoharnesses.domain.errors import DomainError
    from talktoharnesses.domain.models import InteractionAnswer
    from talktoharnesses.providers.adapter import HarnessInteractionRequest, HarnessSession

    adapter = ClaudeAdapter()
    session = HarnessSession(
        conversation_id=uuid4(),
        binding_id=uuid4(),
        kind=HarnessKind.CLAUDE,
        native_session_id="claude-1",
    )
    adapter._session = session  # pyright: ignore[reportPrivateUsage]
    adapter._closed = False  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.set_session("claude-1")  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.begin_turn(uuid4())  # pyright: ignore[reportPrivateUsage]

    task = asyncio.create_task(
        adapter._can_use_tool(  # pyright: ignore[reportPrivateUsage]
            "Bash",
            {"command": "ls"},
            SimpleNamespace(tool_use_id="tool-9"),
        )
    )
    event = await asyncio.wait_for(adapter._event_q.get(), timeout=1.0)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(event, HarnessInteractionRequest)
    interaction_id = event.payload.interaction_id
    await adapter.answer_interaction(
        session,
        InteractionAnswer(interaction_id=interaction_id, decision=ApprovalDecision.ALLOW_ONCE),
    )
    result = await asyncio.wait_for(task, timeout=1.0)
    if isinstance(result, dict):
        assert result.get("behavior") == "allow"  # pyright: ignore[reportUnknownMemberType]
    else:
        assert type(result).__name__ == "PermissionResultAllow"

    with pytest.raises(DomainError) as exc:
        await adapter.answer_interaction(
            session,
            InteractionAnswer(interaction_id=uuid4(), decision=ApprovalDecision.DENY),
        )
    assert exc.value.code is ErrorCode.INVALID_STATE


@pytest.mark.asyncio
async def test_close_cancels_pending_and_disconnect_branches() -> None:
    from talktoharnesses.domain.enums import ApprovalDecision
    from talktoharnesses.providers.adapter import HarnessSession

    adapter = ClaudeAdapter()
    session = HarnessSession(
        conversation_id=uuid4(),
        binding_id=uuid4(),
        kind=HarnessKind.CLAUDE,
        native_session_id="claude-1",
    )
    adapter._session = session  # pyright: ignore[reportPrivateUsage]
    adapter._closed = False  # pyright: ignore[reportPrivateUsage]
    future: asyncio.Future[ApprovalDecision | None] = asyncio.get_running_loop().create_future()
    adapter._pending_interactions[uuid4()] = future  # pyright: ignore[reportPrivateUsage]

    async def disconnect() -> None:
        return None

    adapter._client = SimpleNamespace(disconnect=disconnect)  # type: ignore[assignment]
    await adapter.close(session)
    assert future.done()
    assert future.result() is ApprovalDecision.CANCEL
    await adapter.close(session)

    # __aexit__ close path
    adapter2 = ClaudeAdapter()
    adapter2._session = session  # pyright: ignore[reportPrivateUsage]
    adapter2._closed = False  # pyright: ignore[reportPrivateUsage]
    exited: list[object] = []

    async def aexit(*_a: object) -> None:
        exited.append(True)

    adapter2._client = SimpleNamespace(__aexit__=aexit)  # type: ignore[assignment]
    await adapter2.close(session)
    assert exited


def test_coerce_message_branches() -> None:
    from talktoharnesses.domain.enums import ErrorCode
    from talktoharnesses.domain.errors import DomainError

    adapter = ClaudeAdapter()

    assert adapter._coerce_message({"type": "assistant", "content": []}) == {  # pyright: ignore[reportPrivateUsage]
        "type": "assistant",
        "content": [],
    }

    class _Dump:
        def model_dump(self) -> dict[str, object]:
            return {"type": "result", "subtype": "success"}

    coerced = adapter._coerce_message(_Dump())  # pyright: ignore[reportPrivateUsage]
    assert coerced is not None
    assert coerced["type"] == "result"

    class TextBlock:
        text = "hi"

    class ToolUseBlock:
        id = "t1"
        name = "Bash"
        input = {"x": 1}

    class ThinkingBlock:
        thinking = "hmm"

    class ToolResultBlock:
        tool_use_id = "t1"
        content = "ok"
        is_error = False

    class AssistantMessage:
        content = [TextBlock(), ThinkingBlock(), ToolUseBlock(), ToolResultBlock()]
        model = "claude"
        session_id = "s1"
        message_id = "m1"

    coerced = adapter._coerce_message(AssistantMessage())  # pyright: ignore[reportPrivateUsage]
    assert coerced is not None
    assert coerced["type"] == "assistant"
    assert coerced["content"][0] == {"type": "text", "text": "hi"}
    assert coerced["content"][2]["type"] == "tool_use"

    class ResultMessage:
        subtype = "success"
        session_id = "s1"
        is_error = False
        duration_ms = 10
        duration_api_ms = 5
        num_turns = 1
        stop_reason = "end_turn"
        total_cost_usd = 0.1
        usage = None
        result = "done"
        errors = None

    result_msg = adapter._coerce_message(ResultMessage())  # pyright: ignore[reportPrivateUsage]
    assert result_msg is not None
    assert result_msg["type"] == "result"

    class SystemMessage:
        subtype = "init"
        data = {"a": 1}

    system_msg = adapter._coerce_message(SystemMessage())  # pyright: ignore[reportPrivateUsage]
    assert system_msg is not None
    assert system_msg["subtype"] == "init"

    class RateLimitEvent:
        pass

    assert adapter._coerce_message(RateLimitEvent()) is None  # pyright: ignore[reportPrivateUsage]

    class Unknown:
        pass

    with pytest.raises(DomainError) as exc:
        adapter._coerce_message(Unknown())  # pyright: ignore[reportPrivateUsage]
    assert exc.value.code is ErrorCode.UNSUPPORTED_NATIVE_EVENT
