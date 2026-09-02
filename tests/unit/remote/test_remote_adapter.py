"""RemoteHarnessAdapter wire-contract tests against an in-process fake split."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest
from tth_types.adapter import HarnessSession as WireSession
from tth_types.enums import ErrorCode, HarnessKind, InteractionKind
from tth_types.errors import DomainError
from tth_types.events import InteractionRequestedPayload, TurnStartedPayload, UsageUpdatedPayload
from tth_types.harness import (
    ApprovalRequestPayload,
    HarnessCapabilities,
    HarnessConfiguration,
    InteractionAnswer,
    LaunchSnapshot,
    VersionAdvisory,
)
from tth_types.process import ProcessExitedEvent
from tth_types.split_api import (
    CreateSessionRequest,
    HarnessEventFrame,
    InteractionFrame,
    ProbeRequest,
    ProbeResponse,
    ProcessFrame,
    ProcessSnapshot,
    SessionCreated,
    SplitError,
)

from talktoharnesses.providers.adapter import (
    HarnessInteractionRequest,
    ResumeSessionRequest,
    StartSessionRequest,
    SteerRequest,
    TurnRequest,
)
from talktoharnesses.remote.adapter import RemoteHarnessAdapter
from talktoharnesses.remote.sandbox import SplitEndpoint


class FakeSplit:
    """Minimal in-process split service behind an httpx.MockTransport."""

    def __init__(self, *, kind: HarnessKind = HarnessKind.CLAUDE, pid: int | None = None) -> None:
        self.kind = kind
        self.pid = pid
        self.requests: list[tuple[str, str, Any]] = []
        self.session_id = uuid4()
        self.sse_frames: list[tuple[str, str]] = []
        self.headers_seen: list[dict[str, str]] = []

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(kind=self.kind, version="1.2.3", supports_resume=True)

    def launch(self) -> LaunchSnapshot:
        return LaunchSnapshot(
            resolved_executable="/opt/harness/bin/tool" if self.pid is not None else None,
            harness_version="1.2.3",
            working_directory="/work",
            adapter_version="split-1",
            capabilities=self.capabilities(),
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = request.content.decode() if request.content else ""
        self.requests.append((request.method, path, json.loads(body) if body else None))
        self.headers_seen.append(dict(request.headers))
        if path == "/v1/probe":
            ProbeRequest.model_validate_json(body)
            probe = ProbeResponse(
                capabilities=self.capabilities(),
                launch=self.launch(),
                advisory=VersionAdvisory(
                    status="verified",
                    probed_version="1.2.3",
                    floor_version="1.0.0",
                    latest_verified="1.2.3",
                ),
            )
            return httpx.Response(200, content=probe.model_dump_json())
        if path == "/v1/sessions" and request.method == "POST":
            create = CreateSessionRequest.model_validate_json(body)
            self.session_id = create.session_id
            created = SessionCreated(
                session_id=create.session_id,
                session=WireSession(
                    conversation_id=create.conversation_id,
                    binding_id=create.binding_id,
                    kind=self.kind,
                    native_session_id="native-1",
                    metadata={"split_session_id": str(create.session_id)},
                ),
                launch=self.launch(),
                pid=self.pid,
            )
            return httpx.Response(201, content=created.model_dump_json())
        if path.endswith("/events"):
            payload = b"".join(
                f"event: {name}\nid: {i}\ndata: {data}\n\n".encode()
                for i, (name, data) in enumerate(self.sse_frames, start=1)
            )
            return httpx.Response(
                200, content=payload, headers={"Content-Type": "text/event-stream"}
            )
        if path.endswith("/turns") or path.endswith("/interrupt") or path.endswith("/answers"):
            return httpx.Response(204)
        if path.endswith("/steer"):
            return httpx.Response(200, content='{"accepted": true}')
        if path.endswith("/terminate"):
            return httpx.Response(204)
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(
            404, content=SplitError(code="not_found", message="nope").model_dump_json()
        )


class _Endpoints:
    def __init__(self) -> None:
        self.calls = 0
        self.required_paths: tuple[str, ...] | None = None

    async def endpoint(
        self,
        kind: HarnessKind,
        required_paths: tuple[str, ...] = (),
    ) -> SplitEndpoint:
        self.calls += 1
        self.required_paths = required_paths
        return SplitEndpoint(base_url="http://split.test", token="tok-1")


def _adapter(split: FakeSplit) -> RemoteHarnessAdapter:
    transport = httpx.MockTransport(split.handler)

    class _Client(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    return RemoteHarnessAdapter(
        split.kind,
        _Endpoints(),
        adapter_version="proxy-1",
        client_factory=_Client,
    )


def _config() -> HarnessConfiguration:
    return HarnessConfiguration(kind=HarnessKind.CLAUDE, working_directory="/work")


def _start_request() -> StartSessionRequest:
    split_launch = LaunchSnapshot(
        resolved_executable=None,
        harness_version="1.2.3",
        working_directory="/work",
        adapter_version="proxy-1",
        capabilities=HarnessCapabilities(kind=HarnessKind.CLAUDE, version="1.2.3"),
    )
    return StartSessionRequest(
        conversation_id=uuid4(),
        binding_id=uuid4(),
        configuration=_config(),
        launch=split_launch,
    )


async def test_probe_passes_workspace_paths_to_endpoint_provider() -> None:
    split = FakeSplit()
    transport = httpx.MockTransport(split.handler)

    class _Client(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    endpoints = _Endpoints()
    adapter = RemoteHarnessAdapter(
        split.kind,
        endpoints,
        adapter_version="proxy-1",
        client_factory=_Client,
    )
    await adapter.probe(
        HarnessConfiguration(
            kind=HarnessKind.CLAUDE,
            working_directory="/work",
            workspace_roots=("/work-extra",),
        )
    )
    assert endpoints.required_paths == ("/work", "/work-extra")


async def test_probe_caches_launch_and_advisory() -> None:
    split = FakeSplit()
    adapter = _adapter(split)
    adapter.set_redaction_patterns(("secret",))
    caps = await adapter.probe(_config())
    assert caps.version == "1.2.3"
    launch = adapter.last_probe_launch()
    assert launch is not None and launch.adapter_version == "split-1"
    advisory = adapter.last_probe_advisory()
    assert advisory is not None and advisory.status == "verified"
    method, path, body = split.requests[0]
    assert (method, path) == ("POST", "/v1/probe")
    assert body["redaction_patterns"] == ["secret"]
    assert body["adapter_version"] == "proxy-1"
    assert split.headers_seen[0]["x-tth-split-token"] == "tok-1"


async def test_start_sends_seen_sets_and_creates_handle() -> None:
    split = FakeSplit(pid=4242)
    adapter = _adapter(split)
    adapter.import_seen(frozenset({"n1"}), frozenset({"o1"}))
    session = await adapter.start(_start_request())
    assert session.native_session_id == "native-1"
    assert adapter.process_handle is not None
    assert adapter.process_handle.pid == 4242
    _, _, body = split.requests[0]
    assert body["mode"] == "start"
    assert body["seen_native_ids"] == ["n1"]
    assert body["seen_stream_offsets"] == ["o1"]


async def test_sdk_managed_split_has_no_handle() -> None:
    split = FakeSplit(pid=None)
    adapter = _adapter(split)
    await adapter.start(_start_request())
    assert adapter.process_handle is None


async def test_resume_requires_native_session_id_in_body() -> None:
    split = FakeSplit()
    adapter = _adapter(split)
    request = _start_request()
    await adapter.resume(
        ResumeSessionRequest(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            configuration=request.configuration,
            native_session_id="native-9",
            launch=request.launch,
        )
    )
    _, _, body = split.requests[0]
    assert body["mode"] == "resume"
    assert body["native_session_id"] == "native-9"


async def test_events_stream_updates_seen_mirror_and_routes_process_frames() -> None:
    split = FakeSplit(pid=99)
    turn_id = uuid4()
    split.sse_frames = [
        (
            "harness_event",
            HarnessEventFrame(
                item=TurnStartedPayload(turn_id=turn_id),
                new_native_ids=("n-1",),
            ).model_dump_json(),
        ),
        (
            "interaction",
            InteractionFrame(
                payload=InteractionRequestedPayload(
                    turn_id=turn_id,
                    interaction_id=uuid4(),
                    kind=InteractionKind.APPROVAL,
                    request=ApprovalRequestPayload(tool_name="bash"),
                ),
                provider_correlation={"tool_name": "bash"},
                new_native_ids=("n-2",),
            ).model_dump_json(),
        ),
        (
            "process",
            ProcessFrame(
                event=ProcessExitedEvent(process_id=uuid4(), exit_code=0),
                snapshot=ProcessSnapshot(pid=99, returncode=0),
            ).model_dump_json(),
        ),
        (
            "harness_event",
            HarnessEventFrame(
                item=UsageUpdatedPayload(turn_id=turn_id, input_tokens=5),
            ).model_dump_json(),
        ),
        ("end", '{"reason": "closed"}'),
    ]
    adapter = _adapter(split)
    session = await adapter.start(_start_request())
    items: list[Any] = []
    async for item in adapter.events(session):
        items.append(item)
        # Mirror must be updated before the item is yielded.
        seen, _ = adapter.export_seen()
        if isinstance(item, TurnStartedPayload):
            assert "n-1" in seen
        if isinstance(item, HarnessInteractionRequest):
            assert "n-2" in seen

    assert [type(i).__name__ for i in items] == [
        "TurnStartedPayload",
        "HarnessInteractionRequest",
        "UsageUpdatedPayload",
    ]
    handle = adapter.process_handle
    assert handle is not None
    assert handle.returncode == 0
    process_events = [event async for event in handle.events()]
    assert isinstance(process_events[0], ProcessExitedEvent)


async def test_unary_operations_round_trip() -> None:
    split = FakeSplit()
    adapter = _adapter(split)
    session = await adapter.start(_start_request())
    await adapter.submit(session, TurnRequest(turn_id=uuid4(), prompt="hi"))
    assert await adapter.steer(session, SteerRequest(turn_id=uuid4(), prompt="left"))
    await adapter.interrupt(session)
    await adapter.answer_interaction(session, InteractionAnswer(interaction_id=uuid4()))
    await adapter.close(session)
    methods = [(m, p.rsplit("/", 1)[-1]) for m, p, _ in split.requests[1:]]
    assert methods == [
        ("POST", "turns"),
        ("POST", "steer"),
        ("POST", "interrupt"),
        ("POST", "answers"),
        ("DELETE", str(split.session_id)),
    ]
    # close is idempotent — a second close issues no request.
    count = len(split.requests)
    await adapter.close(session)
    assert len(split.requests) == count


async def test_split_error_round_trips_to_domain_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        error = SplitError(
            code=ErrorCode.PROVIDER_INCOMPATIBLE.value,
            message="resume_unsupported",
            details={"conversation_id": "c1"},
        )
        return httpx.Response(409, content=error.model_dump_json())

    transport = httpx.MockTransport(handler)

    class _Client(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    adapter = RemoteHarnessAdapter(HarnessKind.CLAUDE, _Endpoints(), client_factory=_Client)
    with pytest.raises(DomainError) as excinfo:
        await adapter.probe(_config())
    assert excinfo.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
    assert excinfo.value.message == "resume_unsupported"
    assert excinfo.value.details == {"conversation_id": "c1"}


async def test_unknown_split_error_code_keeps_message_and_details() -> None:
    """A split's request-validation 422 must surface its pydantic message."""

    def handler(request: httpx.Request) -> httpx.Response:
        error = SplitError(
            code="validation_error",
            message="session_id: Extra inputs are not permitted",
            details={"field": "session_id"},
        )
        return httpx.Response(422, content=error.model_dump_json())

    transport = httpx.MockTransport(handler)

    class _Client(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    adapter = RemoteHarnessAdapter(HarnessKind.CLAUDE, _Endpoints(), client_factory=_Client)
    with pytest.raises(DomainError) as excinfo:
        await adapter.probe(_config())
    assert excinfo.value.code is ErrorCode.PROTOCOL_ERROR
    assert "session_id: Extra inputs are not permitted" in excinfo.value.message
    assert "validation_error" in excinfo.value.message
    assert excinfo.value.details == {
        "field": "session_id",
        "status": 422,
        "split_code": "validation_error",
    }


async def test_non_json_split_error_is_generic_protocol_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"<html>bad gateway</html>")

    transport = httpx.MockTransport(handler)

    class _Client(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    adapter = RemoteHarnessAdapter(HarnessKind.CLAUDE, _Endpoints(), client_factory=_Client)
    with pytest.raises(DomainError) as excinfo:
        await adapter.probe(_config())
    assert excinfo.value.code is ErrorCode.PROTOCOL_ERROR
    assert excinfo.value.message == "split returned HTTP 502"
    assert excinfo.value.details == {"status": 502}


async def test_force_terminate_marks_forced_and_posts() -> None:
    split = FakeSplit(pid=7)
    adapter = _adapter(split)
    await adapter.start(_start_request())
    handle = adapter.process_handle
    assert handle is not None
    await handle.force_terminate(reason="interrupt_timeout")
    assert handle.forced
    assert handle.forced_reason == "interrupt_timeout"
    assert split.requests[-1][1].endswith("/terminate")


async def test_close_without_session_is_noop() -> None:
    split = FakeSplit()
    adapter = _adapter(split)
    provisional = WireSession(
        conversation_id=uuid4(),
        binding_id=uuid4(),
        kind=HarnessKind.CLAUDE,
    )
    await adapter.close(provisional)
    assert split.requests == []


async def test_close_can_delete_session_while_create_response_is_pending() -> None:
    create_received = asyncio.Event()
    release_response = asyncio.Event()
    requests: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "POST":
            create = CreateSessionRequest.model_validate_json(request.content)
            create_received.set()
            await release_response.wait()
            session = WireSession(
                conversation_id=create.conversation_id,
                binding_id=create.binding_id,
                kind=HarnessKind.CLAUDE,
            )
            return httpx.Response(
                201,
                content=SessionCreated(
                    session_id=create.session_id,
                    session=session,
                    launch=LaunchSnapshot(
                        harness_version="1",
                        working_directory="/work",
                        adapter_version="split",
                        capabilities=HarnessCapabilities(kind=HarnessKind.CLAUDE, version="1"),
                    ),
                ).model_dump_json(),
            )
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)

    class _Client(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    adapter = RemoteHarnessAdapter(HarnessKind.CLAUDE, _Endpoints(), client_factory=_Client)
    request = _start_request()
    creating = asyncio.create_task(adapter.start(request))
    await create_received.wait()

    await adapter.close(
        WireSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=HarnessKind.CLAUDE,
        )
    )
    delete_path = next(path for method, path in requests if method == "DELETE")
    assert delete_path.startswith("/v1/sessions/")

    release_response.set()
    await creating


async def test_close_after_failed_endpoint_resolution_does_not_resolve_again() -> None:
    """Rollback close() must not re-enter endpoint(): that can restart a failed prepare."""

    class _FailingEndpoints:
        def __init__(self) -> None:
            self.calls = 0

        async def endpoint(
            self,
            kind: HarnessKind,
            required_paths: tuple[str, ...] = (),
        ) -> SplitEndpoint:
            self.calls += 1
            raise DomainError(
                ErrorCode.SANDBOX_UNAVAILABLE,
                "sandbox image build failed",
                details={"kind": kind.value},
            )

    endpoints = _FailingEndpoints()
    adapter = RemoteHarnessAdapter(HarnessKind.CLAUDE, endpoints, adapter_version="proxy-1")
    request = _start_request()

    with pytest.raises(DomainError) as excinfo:
        await adapter.start(request)
    assert excinfo.value.code is ErrorCode.SANDBOX_UNAVAILABLE
    assert endpoints.calls == 1

    await adapter.close(
        WireSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=HarnessKind.CLAUDE,
        )
    )
    assert endpoints.calls == 1


async def test_aclose_releases_probe_only_client() -> None:
    split = FakeSplit()
    adapter = _adapter(split)
    await adapter.probe(_config())
    client = adapter._client  # pyright: ignore[reportPrivateUsage]
    assert client is not None

    await adapter.aclose()

    assert client.is_closed
    assert adapter._client is None  # pyright: ignore[reportPrivateUsage]
    # Idempotent, and also safe when the client was never opened.
    await adapter.aclose()
