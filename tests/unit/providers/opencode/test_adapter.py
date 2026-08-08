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

    async def get(self, path: str) -> FakeResponse:
        if path == "/global/health":
            return FakeResponse(200, {"healthy": True, "version": "1.2.27"})
        if path.startswith("/session/"):
            return FakeResponse(200, {"id": self._session_id, "directory": "/tmp"})
        return FakeResponse(404, {})

    async def post(self, path: str, json: dict[str, Any] | None = None) -> FakeResponse:
        self.posts.append((path, json))
        if path == "/session":
            return FakeResponse(200, {"id": self._session_id, "directory": "/tmp"})
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
        executable_path="/bin/true",
        working_directory="/tmp",
    )


def _launch() -> LaunchSnapshot:
    return LaunchSnapshot(
        resolved_executable="/bin/true",
        harness_version="1.2.27",
        working_directory="/tmp",
        adapter_version="2026.8.0.dev7",
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
