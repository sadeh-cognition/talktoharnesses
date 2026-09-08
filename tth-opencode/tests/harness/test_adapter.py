"""OpenCode adapter unit tests with a fake HTTP client."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from tth_types.adapter import (
    HarnessInteractionRequest,
    ResumeSessionRequest,
    StartSessionRequest,
    TurnRequest,
)
from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError
from tth_types.events import (
    AssistantMessageCompletedPayload,
    AssistantMessageDeltaPayload,
    AssistantMessageStartedPayload,
    ToolCompletedPayload,
    ToolRequestedPayload,
    ToolStartedPayload,
    TurnCompletedPayload,
)
from tth_types.harness import HarnessCapabilities, HarnessConfiguration, LaunchSnapshot

from tth_opencode.harness.adapter import OpenCodeAdapter


@dataclass
class FakeResponse:
    status_code: int
    body: dict[str, Any] | list[Any] | None = None
    chunks: list[bytes] = field(default_factory=list[bytes])
    keep_open: bool = False

    def json(self) -> Any:
        return self.body

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.keep_open:
            while True:
                await asyncio.sleep(3600)

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class FakeHttpClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.posts: list[tuple[str, dict[str, Any] | None]] = []
        self.closed = False
        self._session_id = "sess-1"
        self.message_history: list[dict[str, Any]] = []
        self.session_metadata: dict[str, Any] = {}

    def _session_body(self) -> dict[str, Any]:
        return {"id": self._session_id, "directory": "/tmp", **self.session_metadata}

    async def get(self, path: str) -> FakeResponse:
        if path == "/global/health":
            return FakeResponse(200, {"healthy": True, "version": "1.2.27"})
        if path.endswith("/message"):
            return FakeResponse(200, self.message_history)
        if path.startswith("/session/"):
            return FakeResponse(200, self._session_body())
        return FakeResponse(404, {})

    async def post(self, path: str, json: dict[str, Any] | None = None) -> FakeResponse:
        self.posts.append((path, json))
        if path == "/session":
            return FakeResponse(200, self._session_body())
        if path.endswith("/prompt_async"):
            return FakeResponse(204)
        if path.endswith("/abort"):
            return FakeResponse(200, {})
        return FakeResponse(200, {})

    def stream(self, method: str, path: str) -> FakeResponse:
        del method
        assert path == "/event"
        payload = json.dumps({"type": "server.connected"}).encode()
        return FakeResponse(
            200,
            chunks=[b"data: " + payload + b"\n\n"],
            keep_open=True,
        )

    async def aclose(self) -> None:
        self.closed = True


def _config() -> HarnessConfiguration:
    return HarnessConfiguration(
        kind=HarnessKind.OPENCODE,
        working_directory="/tmp",
        model="opencode/big-pickle",
        effort="high",
    )


def _launch() -> LaunchSnapshot:
    return LaunchSnapshot(
        resolved_executable="/bin/true",
        harness_version="1.2.27",
        working_directory="/tmp",
        adapter_version="2026.8.1",
        capabilities=HarnessCapabilities(kind=HarnessKind.OPENCODE, version="1.2.27"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_metadata",
    [
        {},
        {
            "path": "",
            "cost": 0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        },
    ],
)
async def test_start_and_complete_turn(
    monkeypatch: pytest.MonkeyPatch, session_metadata: dict[str, Any]
) -> None:
    async def fake_probe(config: HarnessConfiguration):
        from tth_opencode.harness.compatibility import match_release

        release = match_release("1.2.27", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("tth_opencode.harness.adapter.probe_opencode", fake_probe)
    clients: list[FakeHttpClient] = []

    def factory(base_url: str) -> FakeHttpClient:
        client = FakeHttpClient(base_url)
        client.session_metadata = session_metadata
        clients.append(client)
        return client

    adapter = OpenCodeAdapter(http_client_factory=factory)
    adapter.prepare_port(19501)
    await adapter.probe(_config())
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(),
            launch=_launch(),
        )
    )
    assert session.native_session_id == "sess-1"
    await adapter.submit(session, TurnRequest(turn_id=uuid4(), prompt="hi"))
    assert any(path.endswith("/prompt_async") for path, _ in clients[0].posts)
    prompt = next(body for path, body in clients[0].posts if path.endswith("/prompt_async"))
    assert prompt is not None
    assert prompt["variant"] == "high"
    for event_type, properties in (
        (
            "message.part.updated",
            {
                "part": {
                    "id": "part-read",
                    "sessionID": "sess-1",
                    "messageID": "m1",
                    "type": "tool",
                    "callID": "call-read",
                    "tool": "read",
                    "state": {
                        "status": "completed",
                        "input": {"filePath": "/tmp/file"},
                        "output": "file contents",
                    },
                }
            },
        ),
        (
            "message.part.delta",
            {
                "sessionID": "sess-1",
                "messageID": "m1",
                "partID": "p1",
                "field": "text",
                "delta": "Hello",
            },
        ),
        ("session.idle", {"sessionID": "sess-1"}),
    ):
        event = {"type": event_type, "properties": properties}
        if session_metadata:
            event["id"] = f"evt_{event_type}"
        await adapter._dispatch_sse(None, json.dumps(event))  # pyright: ignore[reportPrivateUsage]
    stream = adapter.events(session)
    emitted = [await asyncio.wait_for(anext(stream), timeout=1.0) for _ in range(7)]
    assert [type(event) for event in emitted] == [
        ToolRequestedPayload,
        ToolStartedPayload,
        ToolCompletedPayload,
        AssistantMessageStartedPayload,
        AssistantMessageDeltaPayload,
        AssistantMessageCompletedPayload,
        TurnCompletedPayload,
    ]
    assert isinstance(emitted[2], ToolCompletedPayload)
    assert emitted[2].tool_name == "read"
    assert isinstance(emitted[5], AssistantMessageCompletedPayload)
    assert emitted[5].text == "Hello"
    await adapter.close(session)
    assert clients[0].closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("recovers", [True, False])
async def test_stalled_health_request_is_retried(
    monkeypatch: pytest.MonkeyPatch, recovers: bool
) -> None:
    async def fake_probe(config: HarnessConfiguration):
        from tth_opencode.harness.compatibility import match_release

        release = match_release("1.2.27", platform="linux")
        return release.to_harness_capabilities(), release

    class StalledHealthClient(FakeHttpClient):
        health_calls = 0
        cancelled = False

        async def get(self, path: str) -> FakeResponse:
            if path == "/global/health":
                self.health_calls += 1
                if self.health_calls == 1:
                    try:
                        await asyncio.Event().wait()
                    finally:
                        self.cancelled = True
                if not recovers:
                    raise TimeoutError
            return await super().get(path)

    monkeypatch.setattr("tth_opencode.harness.adapter.probe_opencode", fake_probe)
    client = StalledHealthClient("http://127.0.0.1:19501")
    adapter = OpenCodeAdapter(http_client_factory=lambda _: client)
    adapter.prepare_port(19501)
    await adapter.probe(_config())
    request = StartSessionRequest(
        conversation_id=uuid4(),
        binding_id=uuid4(),
        configuration=_config(),
        launch=_launch(),
    )
    if recovers:
        session = await asyncio.wait_for(adapter.start(request), timeout=3.0)
        assert session.native_session_id == "sess-1"
        assert client.health_calls == 2
        await adapter.close(session)
    else:
        with pytest.raises(DomainError, match="health check timed out.*global/health") as exc:
            await asyncio.wait_for(adapter.start(request), timeout=10.0)
        assert exc.value.code is ErrorCode.RUNTIME_TIMEOUT
        assert exc.value.details["error"] == "TimeoutError"
        assert client.posts == []
        await client.aclose()
    assert client.cancelled


@pytest.mark.asyncio
async def test_permission_events_are_filtered_by_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_probe(config: HarnessConfiguration):
        from tth_opencode.harness.compatibility import match_release

        release = match_release("1.2.27", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("tth_opencode.harness.adapter.probe_opencode", fake_probe)
    adapter = OpenCodeAdapter(http_client_factory=FakeHttpClient)
    adapter.prepare_port(19502)
    await adapter.probe(_config())
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(),
            launch=_launch(),
        )
    )
    await adapter.submit(session, TurnRequest(turn_id=uuid4(), prompt="hi"))
    for session_id, permission_id in (("other", "foreign"), ("sess-1", "own")):
        await adapter._dispatch_sse(  # pyright: ignore[reportPrivateUsage]
            None,
            json.dumps(
                {
                    "type": "permission.asked",
                    "properties": {
                        "sessionID": session_id,
                        "permissionID": permission_id,
                        "tool": "shell",
                    },
                }
            ),
        )
    event = await asyncio.wait_for(anext(adapter.events(session)), timeout=1.0)
    assert isinstance(event, HarnessInteractionRequest)
    assert event.provider_correlation == {"permission_id": "own"}
    await adapter.close(session)


class _DisconnectingResponse(FakeResponse):
    def __init__(self, disconnect: asyncio.Event) -> None:
        payload = json.dumps({"type": "server.connected"}).encode()
        super().__init__(200, chunks=[b"data: " + payload + b"\n\n"])
        self.disconnect = disconnect

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        async for chunk in super().aiter_bytes():
            yield chunk
        await self.disconnect.wait()
        raise OSError("disconnected")


class _ReconnectingHttpClient(FakeHttpClient):
    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        self.disconnect = asyncio.Event()
        self.stream_calls = 0

    def stream(self, method: str, path: str) -> FakeResponse:
        self.stream_calls += 1
        if self.stream_calls == 1:
            return _DisconnectingResponse(self.disconnect)
        return super().stream(method, path)


@pytest.mark.asyncio
async def test_sse_disconnect_replaces_stream_task(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_probe(config: HarnessConfiguration):
        from tth_opencode.harness.compatibility import match_release

        release = match_release("1.2.27", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("tth_opencode.harness.adapter.probe_opencode", fake_probe)
    clients: list[_ReconnectingHttpClient] = []

    def factory(base_url: str) -> _ReconnectingHttpClient:
        client = _ReconnectingHttpClient(base_url)
        clients.append(client)
        return client

    adapter = OpenCodeAdapter(http_client_factory=factory)
    adapter.prepare_port(19503)
    await adapter.probe(_config())
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(),
            launch=_launch(),
        )
    )
    clients[0].disconnect.set()
    for _ in range(50):
        if clients[0].stream_calls >= 2:
            break
        await asyncio.sleep(0.01)
    assert clients[0].stream_calls == 2
    await adapter.close(session)


@pytest.mark.asyncio
async def test_answer_interaction_pending_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    from tth_types.enums import ApprovalDecision, ErrorCode
    from tth_types.errors import DomainError
    from tth_types.harness import InteractionAnswer

    async def fake_probe(config: HarnessConfiguration):
        from tth_opencode.harness.compatibility import match_release

        release = match_release("1.2.27", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("tth_opencode.harness.adapter.probe_opencode", fake_probe)
    client = FakeHttpClient("http://127.0.0.1")
    adapter = OpenCodeAdapter(http_client_factory=lambda base_url: client)
    adapter.prepare_port(19504)
    await adapter.probe(_config())
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(),
            launch=_launch(),
        )
    )
    await adapter.submit(session, TurnRequest(turn_id=uuid4(), prompt="hi"))
    await adapter._dispatch_sse(  # pyright: ignore[reportPrivateUsage]
        None,
        json.dumps(
            {
                "type": "permission.asked",
                "properties": {
                    "sessionID": "sess-1",
                    "permissionID": "perm-42",
                    "tool": "shell",
                    "title": "Run shell",
                },
            }
        ),
    )
    event = await asyncio.wait_for(anext(adapter.events(session)), timeout=1.0)
    assert isinstance(event, HarnessInteractionRequest)
    interaction_id = event.payload.interaction_id

    await adapter.answer_interaction(
        session,
        InteractionAnswer(interaction_id=interaction_id, decision=ApprovalDecision.ALLOW_ONCE),
    )
    assert any(
        path.endswith("/permissions/perm-42") and body == {"response": "once"}
        for path, body in client.posts
    )

    with pytest.raises(DomainError) as exc:
        await adapter.answer_interaction(
            session,
            InteractionAnswer(interaction_id=uuid4(), decision=ApprovalDecision.DENY),
        )
    assert exc.value.code is ErrorCode.INVALID_STATE
    await adapter.close(session)


@pytest.mark.asyncio
async def test_retry_startup_and_close_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    from tth_types.enums import ErrorCode
    from tth_types.errors import DomainError

    async def fake_probe(config: HarnessConfiguration):
        from tth_opencode.harness.compatibility import match_release

        release = match_release("1.2.27", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("tth_opencode.harness.adapter.probe_opencode", fake_probe)
    client = FakeHttpClient("http://127.0.0.1")
    adapter = OpenCodeAdapter(http_client_factory=lambda base_url: client)
    adapter.prepare_port(19505)
    await adapter.probe(_config())

    # No dead process → no retry argv.
    assert (
        await adapter.retry_startup(
            DomainError(ErrorCode.RUNTIME_TIMEOUT, "bind race"),
        )
        is None
    )

    class _DeadProcess:
        returncode = 1

    adapter._process = _DeadProcess()  # type: ignore[assignment]
    argv = await adapter.retry_startup(DomainError(ErrorCode.RUNTIME_TIMEOUT, "bind race"))
    assert argv is not None
    assert any("--port" in part or part.isdigit() for part in argv)

    # Wrong error code → no retry.
    adapter._process = _DeadProcess()  # type: ignore[assignment]
    assert await adapter.retry_startup(DomainError(ErrorCode.INVALID_STATE, "nope")) is None
    adapter._process = None  # type: ignore[assignment]

    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(),
            launch=_launch(),
        )
    )
    await adapter.close(session)
    await adapter.close(session)


def test_bind_process_redaction_seen_and_build_argv() -> None:
    adapter = OpenCodeAdapter(http_client_factory=lambda base_url: FakeHttpClient(base_url))
    handle = object()
    adapter.bind_process(handle)  # type: ignore[arg-type]
    assert adapter._process is handle  # pyright: ignore[reportPrivateUsage]
    adapter.set_redaction_patterns(("SECRET",))
    adapter.import_seen(frozenset({"n"}), frozenset({"o"}))
    native, offsets = adapter.export_seen()
    assert "n" in native and "o" in offsets
    argv = adapter.build_argv(_config())
    assert any(part.isdigit() or part.startswith("--") for part in argv)
    assert adapter._port is not None  # pyright: ignore[reportPrivateUsage]


def test_only_root_session_is_success_terminal() -> None:
    adapter = OpenCodeAdapter()
    adapter._normalizer.set_session("parent")  # pyright: ignore[reportPrivateUsage]

    assert adapter._is_success_terminal(  # pyright: ignore[reportPrivateUsage]
        {"type": "session.idle", "properties": {"sessionID": "parent"}}
    )
    assert not adapter._is_success_terminal(  # pyright: ignore[reportPrivateUsage]
        {"type": "session.idle", "properties": {"sessionID": "child"}}
    )
    assert not adapter._is_success_terminal(  # pyright: ignore[reportPrivateUsage]
        {"type": "session.status", "properties": {"status": {"type": "idle"}}}
    )


@pytest.mark.asyncio
async def test_terminal_reconciles_usage_history_before_emitting_completion() -> None:
    from tth_types.events import CostUpdatedPayload, UsageUpdatedPayload

    client = FakeHttpClient("http://127.0.0.1")
    client.message_history = [
        {
            "info": {"id": "old-message"},
            "parts": [
                {
                    "id": "old-step",
                    "sessionID": "sess-1",
                    "messageID": "old-message",
                    "type": "step-finish",
                    "reason": "stop",
                    "cost": 1.0,
                    "tokens": {
                        "total": 999,
                        "input": 999,
                        "output": 0,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                }
            ],
        },
        {"info": {"id": "root-message"}, "parts": []},
        {
            "info": {"id": "answer"},
            "parts": [
                {
                    "id": "current-step",
                    "sessionID": "sess-1",
                    "messageID": "answer",
                    "type": "step-finish",
                    "reason": "stop",
                    "cost": 0.1,
                    "tokens": {
                        "total": 12,
                        "input": 10,
                        "output": 2,
                        "reasoning": 1,
                        "cache": {"read": 4, "write": 3},
                    },
                }
            ],
        },
    ]
    adapter = OpenCodeAdapter(http_client_factory=lambda _base_url: client)
    adapter._client = client  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.set_session("sess-1")  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.begin_turn(  # pyright: ignore[reportPrivateUsage]
        uuid4(), root_message_id="root-message"
    )

    await adapter._dispatch_sse(  # pyright: ignore[reportPrivateUsage]
        None,
        json.dumps(
            {
                "type": "session.idle",
                "properties": {"sessionID": "sess-1"},
            }
        ),
    )
    events: list[object] = []
    while not adapter._event_q.empty():  # pyright: ignore[reportPrivateUsage]
        events.append(adapter._event_q.get_nowait())  # pyright: ignore[reportPrivateUsage]
    usage = next(event for event in events if isinstance(event, UsageUpdatedPayload))
    assert usage.input_tokens == 10
    cost = next(event for event in events if isinstance(event, CostUpdatedPayload))
    assert cost.cost == "0.1"
    assert cost.currency == "USD"
    assert [getattr(event, "type", None) for event in events][-3:] == [
        "usage_updated",
        "cost_updated",
        "turn_completed",
    ]


@pytest.mark.asyncio
async def test_terminal_keeps_live_usage_when_history_reconciliation_fails() -> None:
    from tth_types.events import UsageUpdatedPayload

    class FailingHistoryClient(FakeHttpClient):
        async def get(self, path: str) -> FakeResponse:
            if path.endswith("/message"):
                raise OSError("history unavailable")
            return await super().get(path)

    client = FailingHistoryClient("http://127.0.0.1")
    adapter = OpenCodeAdapter(http_client_factory=lambda _base_url: client)
    adapter._client = client  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.set_session("sess-1")  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.begin_turn(uuid4())  # pyright: ignore[reportPrivateUsage]
    await adapter._dispatch_sse(  # pyright: ignore[reportPrivateUsage]
        None,
        json.dumps(
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "id": "step-1",
                        "sessionID": "sess-1",
                        "messageID": "answer",
                        "type": "step-finish",
                        "reason": "stop",
                        "cost": 0.1,
                        "tokens": {
                            "total": 12,
                            "input": 10,
                            "output": 2,
                            "reasoning": 1,
                            "cache": {"read": 4, "write": 3},
                        },
                    }
                },
            }
        ),
    )
    await adapter._dispatch_sse(  # pyright: ignore[reportPrivateUsage]
        None,
        json.dumps(
            {
                "type": "session.idle",
                "properties": {"sessionID": "sess-1"},
            }
        ),
    )
    events: list[object] = []
    while not adapter._event_q.empty():  # pyright: ignore[reportPrivateUsage]
        events.append(adapter._event_q.get_nowait())  # pyright: ignore[reportPrivateUsage]
    assert any(isinstance(event, UsageUpdatedPayload) for event in events)
    assert getattr(events[-1], "type", None) == "turn_completed"


@pytest.mark.asyncio
async def test_dispatch_sse_and_reconnect_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    from tth_types.adapter import HarnessSession
    from tth_types.enums import ErrorCode
    from tth_types.errors import DomainError
    from tth_types.events import TurnOutcomeUnknownPayload

    async def fake_probe(config: HarnessConfiguration):
        from tth_opencode.harness.compatibility import match_release

        release = match_release("1.2.27", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("tth_opencode.harness.adapter.probe_opencode", fake_probe)
    client = FakeHttpClient("http://127.0.0.1")
    adapter = OpenCodeAdapter(http_client_factory=lambda base_url: client)
    adapter.prepare_port(19506)
    await adapter.probe(_config())
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(),
            launch=_launch(),
        )
    )
    adapter._normalizer.begin_turn(uuid4())  # pyright: ignore[reportPrivateUsage]

    await adapter._dispatch_sse("server.connected", "")  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(DomainError):
        await adapter._dispatch_sse(None, "{not-json")  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(DomainError):
        await adapter._dispatch_sse(None, "[]")  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(DomainError):
        await adapter._dispatch_sse(  # pyright: ignore[reportPrivateUsage]
            None,
            json.dumps({"type": "permission.asked", "properties": {"permissionID": "p"}}),
        )

    # Flat envelope without properties.
    await adapter._dispatch_sse(  # pyright: ignore[reportPrivateUsage]
        None,
        json.dumps(
            {
                "type": "message.part.delta",
                "sessionID": "sess-1",
                "messageID": "m1",
                "partID": "p1",
                "field": "text",
                "delta": "hi",
            }
        ),
    )

    # Reconnect when process already dead → outcome unknown.
    class _Dead:
        returncode = 9

    adapter._process = _Dead()  # type: ignore[assignment]
    adapter._closed = False  # pyright: ignore[reportPrivateUsage]
    await adapter._reconnect_resync()  # pyright: ignore[reportPrivateUsage]
    drained: list[object] = []
    while True:
        try:
            drained.append(adapter._event_q.get_nowait())  # pyright: ignore[reportPrivateUsage]
        except Exception:
            break
    assert any(isinstance(item, TurnOutcomeUnknownPayload) for item in drained)

    with pytest.raises(DomainError):
        adapter._require_session(  # pyright: ignore[reportPrivateUsage]
            HarnessSession(
                conversation_id=uuid4(),
                binding_id=uuid4(),
                kind=HarnessKind.OPENCODE,
            )
        )
    adapter._raise_http(FakeResponse(status_code=200), "ok")  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(DomainError) as http_exc:
        adapter._raise_http(FakeResponse(status_code=500), "boom")  # pyright: ignore[reportPrivateUsage]
    assert http_exc.value.code is ErrorCode.PROTOCOL_ERROR
    await adapter.close(session)


def test_opencode_model_ref_parsing() -> None:
    from tth_types.enums import ErrorCode
    from tth_types.errors import DomainError

    from tth_opencode.harness.adapter import (
        _opencode_model_ref,  # pyright: ignore[reportPrivateUsage]
    )

    assert _opencode_model_ref("openai/gpt-5") == {
        "providerID": "openai",
        "modelID": "gpt-5",
    }
    assert _opencode_model_ref("anthropic/claude-sonnet-4/extra") == {
        "providerID": "anthropic",
        "modelID": "claude-sonnet-4/extra",
    }
    with pytest.raises(DomainError) as missing:
        _opencode_model_ref("gpt-5")
    assert missing.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
    with pytest.raises(DomainError):
        _opencode_model_ref("/only-model")
    with pytest.raises(DomainError):
        _opencode_model_ref("provider/")


@pytest.mark.asyncio
async def test_question_asked_and_submit_model_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    from tth_types.adapter import SteerRequest
    from tth_types.enums import ApprovalDecision, ErrorCode
    from tth_types.errors import DomainError
    from tth_types.harness import InteractionAnswer

    from tth_opencode.harness.compatibility import match_release

    async def fake_probe(config: HarnessConfiguration):
        release = match_release("1.2.27", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("tth_opencode.harness.adapter.probe_opencode", fake_probe)
    client = FakeHttpClient("http://127.0.0.1")
    adapter = OpenCodeAdapter(http_client_factory=lambda base_url: client)
    adapter.prepare_port(19507)
    config = HarnessConfiguration(
        kind=HarnessKind.OPENCODE,
        working_directory="/tmp",
        model="openai/gpt-test",
        mode="build",
    )
    await adapter.probe(config)
    launch = LaunchSnapshot(
        resolved_executable="/bin/true",
        harness_version="1.2.27",
        working_directory="/tmp",
        adapter_version="2026.8.1",
        capabilities=HarnessCapabilities(kind=HarnessKind.OPENCODE, version="1.2.27"),
        model="openai/gpt-test",
        mode="build",
    )
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=config,
            launch=launch,
        )
    )
    turn_id = uuid4()
    await adapter.submit(session, TurnRequest(turn_id=turn_id, prompt="hi"))
    prompt_posts = [body for path, body in client.posts if path.endswith("/prompt_async")]
    assert prompt_posts[-1] is not None
    assert prompt_posts[-1]["model"] == {"providerID": "openai", "modelID": "gpt-test"}
    assert prompt_posts[-1]["agent"] == "build"
    assert await adapter.steer(session, SteerRequest(turn_id=turn_id, prompt="more")) is False

    adapter._normalizer.begin_turn(turn_id)  # pyright: ignore[reportPrivateUsage]
    await adapter._dispatch_sse(  # pyright: ignore[reportPrivateUsage]
        None,
        json.dumps(
            {
                "type": "question.asked",
                "properties": {
                    "sessionID": "sess-1",
                    "id": "q-1",
                    "questions": [{"header": "Pick", "options": [{"label": "A", "value": "a"}]}],
                },
            }
        ),
    )
    item = adapter._event_q.get_nowait()  # pyright: ignore[reportPrivateUsage]
    assert isinstance(item, HarnessInteractionRequest)
    await adapter.answer_interaction(
        session,
        InteractionAnswer(
            interaction_id=item.payload.interaction_id,
            decision=ApprovalDecision.ALLOW_ONCE,
            answers={"question-1": ["a"]},
        ),
    )
    reply_posts = [body for path, body in client.posts if path.endswith("/question/q-1/reply")]
    assert reply_posts

    with pytest.raises(DomainError) as missing_id:
        await adapter._handle_question({})  # pyright: ignore[reportPrivateUsage]
    assert missing_id.value.code is ErrorCode.PROTOCOL_ERROR
    await adapter.close(session)


async def _probe_opencode(config: HarnessConfiguration):
    del config
    from tth_opencode.harness.compatibility import match_release

    release = match_release("1.2.27", platform="linux")
    return release.to_harness_capabilities(), release


@pytest.mark.asyncio
async def test_yolo_preserves_plan_mode_permissions_on_create_and_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tth_opencode.harness.adapter.probe_opencode",
        _probe_opencode,
    )
    client = FakeHttpClient("http://127.0.0.1")
    adapter = OpenCodeAdapter(http_client_factory=lambda base_url: client)
    adapter.prepare_port(19508)
    yolo = HarnessConfiguration(
        kind=HarnessKind.OPENCODE,
        working_directory="/tmp",
        mode="plan",
        yolo=True,
    )
    await adapter.probe(yolo)
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=yolo,
            launch=_launch(),
        )
    )
    create = next(body for path, body in client.posts if path == "/session")
    assert create == {"directory": "/tmp"}
    await adapter.submit(session, TurnRequest(turn_id=uuid4(), prompt="plan this"))
    prompt = next(body for path, body in client.posts if path.endswith("/prompt_async"))
    assert prompt is not None
    assert prompt["agent"] == "plan"
    await adapter.close(session)

    resume_client = FakeHttpClient("http://127.0.0.1")
    resume_adapter = OpenCodeAdapter(http_client_factory=lambda base_url: resume_client)
    resume_adapter.prepare_port(19509)
    await resume_adapter.probe(yolo)
    resumed = await resume_adapter.resume(
        ResumeSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=yolo,
            native_session_id="sess-1",
            launch=_launch(),
        )
    )
    await resume_adapter.close(resumed)


@pytest.mark.asyncio
async def test_yolo_keeps_questions_interactive_and_answers_child_session_approvals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tth_types.harness import InteractionAnswer

    monkeypatch.setattr(
        "tth_opencode.harness.adapter.probe_opencode",
        _probe_opencode,
    )
    client = FakeHttpClient("http://127.0.0.1")
    adapter = OpenCodeAdapter(http_client_factory=lambda base_url: client)
    adapter.prepare_port(19511)
    yolo = HarnessConfiguration(
        kind=HarnessKind.OPENCODE,
        working_directory="/tmp",
        yolo=True,
    )
    await adapter.probe(yolo)
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=yolo,
            launch=_launch(),
        )
    )
    await adapter.submit(session, TurnRequest(turn_id=uuid4(), prompt="hi"))
    await adapter._dispatch_sse(  # pyright: ignore[reportPrivateUsage]
        None,
        json.dumps(
            {
                "type": "permission.asked",
                "properties": {
                    "sessionID": "child-session",
                    "permissionID": "perm-yolo",
                    "tool": "shell",
                },
            }
        ),
    )
    assert adapter._event_q.empty()  # pyright: ignore[reportPrivateUsage]
    assert (
        "/permission/perm-yolo/reply",
        {"reply": "once"},
    ) in client.posts
    await adapter._dispatch_sse(  # pyright: ignore[reportPrivateUsage]
        None,
        json.dumps(
            {
                "type": "question.asked",
                "properties": {
                    "sessionID": "sess-1",
                    "id": "q-yolo",
                    "questions": [{"header": "Pick", "options": [{"label": "A", "value": "a"}]}],
                },
            }
        ),
    )
    item = adapter._event_q.get_nowait()  # pyright: ignore[reportPrivateUsage]
    assert isinstance(item, HarnessInteractionRequest)
    await adapter.answer_interaction(
        session,
        InteractionAnswer(
            interaction_id=item.payload.interaction_id,
            answers={"question-1": ["a"]},
        ),
    )
    assert any(path.endswith("/question/q-yolo/reply") for path, _ in client.posts)
    await adapter.close(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("mcp_status", ["connected", "failed", "needs_auth"])
async def test_mcp_attachment_on_create_and_resume(resume: bool, mcp_status: str) -> None:
    from tth_types.harness import HarnessMcpHeader, HarnessMcpServer

    from tth_opencode.harness.compatibility import match_release

    class McpClient(FakeHttpClient):
        async def post(self, path: str, json: dict[str, Any] | None = None) -> FakeResponse:
            if path == "/mcp":
                self.posts.append((path, json))
                assert json is not None
                return FakeResponse(200, {json["name"]: {"status": mcp_status}})
            return await super().post(path, json)

    client = McpClient("http://localhost")
    adapter = OpenCodeAdapter(http_client_factory=lambda _: client)
    adapter.prepare_port(19501)
    release = match_release("1.2.27", platform="linux")
    assert release.to_harness_capabilities().supports_mcp_servers
    adapter._release = release  # pyright: ignore[reportPrivateUsage]
    config = _config().model_copy(
        update={
            "mcp_servers": (
                HarnessMcpServer(
                    name="agentbahn_wiki",
                    url="http://host/mcp/wiki",
                    headers=(HarnessMcpHeader(name="Authorization", value="Bearer secret"),),
                ),
                HarnessMcpServer(name="mnemosyne", url="http://host/mcp/memory"),
            )
        }
    )
    fields = {
        "conversation_id": uuid4(),
        "binding_id": uuid4(),
        "configuration": config,
        "launch": _launch(),
    }
    try:
        if resume:
            operation = adapter.resume(
                ResumeSessionRequest.model_validate({**fields, "native_session_id": "sess-1"})
            )
        else:
            operation = adapter.start(StartSessionRequest.model_validate(fields))
        if mcp_status == "connected":
            session = await operation
            assert session.native_session_id == "sess-1"
            assert [body["name"] for path, body in client.posts if path == "/mcp" and body] == [
                "agentbahn_wiki",
                "mnemosyne",
            ]
        else:
            with pytest.raises(DomainError, match="could not connect to MCP server") as error:
                await operation
            assert error.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
            assert "secret" not in str(error.value)
            assert not any(path == "/session" for path, _ in client.posts)
        assert client.posts[0] == (
            "/mcp",
            {
                "name": "agentbahn_wiki",
                "config": {
                    "type": "remote",
                    "url": "http://host/mcp/wiki",
                    "headers": {"Authorization": "Bearer secret"},
                    "oauth": False,
                },
            },
        )
    finally:
        await adapter._close_http()  # pyright: ignore[reportPrivateUsage]
