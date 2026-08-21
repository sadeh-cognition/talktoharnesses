"""OpenCode adapter unit tests with a fake HTTP client."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.models import HarnessCapabilities, HarnessConfiguration, LaunchSnapshot
from talktoharnesses.providers.adapter import (
    HarnessInteractionRequest,
    ResumeSessionRequest,
    StartSessionRequest,
    TurnRequest,
)
from talktoharnesses.providers.opencode.adapter import OpenCodeAdapter


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

    def _session_body(self) -> dict[str, Any]:
        return {"id": self._session_id, "directory": "/tmp"}

    async def get(self, path: str) -> FakeResponse:
        if path == "/global/health":
            return FakeResponse(200, {"healthy": True, "version": "1.2.27"})
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
async def test_start_and_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_probe(config: HarnessConfiguration):
        from talktoharnesses.providers.opencode.compatibility import match_release

        release = match_release("1.2.27", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("talktoharnesses.providers.opencode.adapter.probe_opencode", fake_probe)
    clients: list[FakeHttpClient] = []

    def factory(base_url: str) -> FakeHttpClient:
        client = FakeHttpClient(base_url)
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
    await adapter.close(session)
    assert clients[0].closed is True


@pytest.mark.asyncio
async def test_permission_events_are_filtered_by_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_probe(config: HarnessConfiguration):
        from talktoharnesses.providers.opencode.compatibility import match_release

        release = match_release("1.2.27", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("talktoharnesses.providers.opencode.adapter.probe_opencode", fake_probe)
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
        from talktoharnesses.providers.opencode.compatibility import match_release

        release = match_release("1.2.27", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("talktoharnesses.providers.opencode.adapter.probe_opencode", fake_probe)
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
    from talktoharnesses.domain.enums import ApprovalDecision, ErrorCode
    from talktoharnesses.domain.errors import DomainError
    from talktoharnesses.domain.models import InteractionAnswer

    async def fake_probe(config: HarnessConfiguration):
        from talktoharnesses.providers.opencode.compatibility import match_release

        release = match_release("1.2.27", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("talktoharnesses.providers.opencode.adapter.probe_opencode", fake_probe)
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
    from talktoharnesses.domain.enums import ErrorCode
    from talktoharnesses.domain.errors import DomainError

    async def fake_probe(config: HarnessConfiguration):
        from talktoharnesses.providers.opencode.compatibility import match_release

        release = match_release("1.2.27", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("talktoharnesses.providers.opencode.adapter.probe_opencode", fake_probe)
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


@pytest.mark.asyncio
async def test_dispatch_sse_and_reconnect_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    from talktoharnesses.domain.enums import ErrorCode
    from talktoharnesses.domain.errors import DomainError
    from talktoharnesses.domain.events import TurnOutcomeUnknownPayload
    from talktoharnesses.providers.adapter import HarnessSession

    async def fake_probe(config: HarnessConfiguration):
        from talktoharnesses.providers.opencode.compatibility import match_release

        release = match_release("1.2.27", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("talktoharnesses.providers.opencode.adapter.probe_opencode", fake_probe)
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
    from talktoharnesses.domain.enums import ErrorCode
    from talktoharnesses.domain.errors import DomainError
    from talktoharnesses.providers.opencode.adapter import (
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
    from talktoharnesses.domain.enums import ApprovalDecision, ErrorCode
    from talktoharnesses.domain.errors import DomainError
    from talktoharnesses.domain.models import InteractionAnswer
    from talktoharnesses.providers.adapter import SteerRequest
    from talktoharnesses.providers.opencode.compatibility import match_release

    async def fake_probe(config: HarnessConfiguration):
        release = match_release("1.2.27", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("talktoharnesses.providers.opencode.adapter.probe_opencode", fake_probe)
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
    from talktoharnesses.providers.opencode.compatibility import match_release

    release = match_release("1.2.27", platform="linux")
    return release.to_harness_capabilities(), release


@pytest.mark.asyncio
async def test_yolo_preserves_plan_mode_permissions_on_create_and_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "talktoharnesses.providers.opencode.adapter.probe_opencode",
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
    from talktoharnesses.domain.models import InteractionAnswer

    monkeypatch.setattr(
        "talktoharnesses.providers.opencode.adapter.probe_opencode",
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
