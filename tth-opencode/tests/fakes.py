"""Fake native transports/SDK surfaces for the adapter contract suite."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from json import dumps
from typing import Any
from uuid import uuid4

from tth_types.adapter import HarnessAdapter
from tth_types.enums import HarnessKind
from tth_types.harness import HarnessCapabilities, HarnessConfiguration

from tth_opencode.harness.adapter import OpenCodeAdapter


@dataclass
class _HttpResponse:
    status_code: int
    body: Any = None
    chunks: list[bytes] = field(default_factory=list[bytes])

    def json(self) -> Any:
        return self.body

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def __aenter__(self) -> _HttpResponse:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


@dataclass
class _OpenStreamResponse(_HttpResponse):
    """SSE response that stays open until the consumer is cancelled."""

    events: asyncio.Queue[bytes] = field(default_factory=lambda: asyncio.Queue[bytes]())

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        while True:
            yield await self.events.get()


class _FakeOpenCodeHttp:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.session_id = f"oc-{uuid4()}"
        self.closed = False
        self.events: asyncio.Queue[bytes] = asyncio.Queue()

    async def get(self, path: str) -> _HttpResponse:
        if path == "/global/health":
            return _HttpResponse(200, {"healthy": True, "version": "1.2.27"})
        if path.startswith("/session/"):
            sid = path.rsplit("/", 1)[-1]
            if sid != self.session_id and sid != self.session_id:
                # Allow resume of known id only; create path sets session_id.
                return _HttpResponse(200, {"id": sid})
            return _HttpResponse(200, {"id": sid})
        return _HttpResponse(404, {})

    async def post(self, path: str, json: dict[str, Any] | None = None) -> _HttpResponse:
        del json
        if path == "/session":
            return _HttpResponse(200, {"id": self.session_id})
        if path.endswith("/prompt_async"):
            payload = {
                "type": "session.status",
                "properties": {"sessionID": self.session_id, "status": {"type": "idle"}},
            }
            self.events.put_nowait(f"data: {dumps(payload)}\n\n".encode())
        return _HttpResponse(200, {"id": "ok"})

    def stream(self, method: str, path: str) -> _HttpResponse:
        del method, path
        payload = b'{"type":"server.connected"}'
        # Keep stream open after the connected event so the SSE task is not torn down
        # before the adapter finishes start/submit.
        return _OpenStreamResponse(
            200,
            chunks=[b"data: " + payload + b"\n\n"],
            events=self.events,
        )

    async def aclose(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# ACP fake process for Grok/Cursor
# ---------------------------------------------------------------------------


def _patch_probe(monkeypatch: Any, kind: HarnessKind) -> None:
    assert kind is HarnessKind.OPENCODE

    async def probe_opencode(config: HarnessConfiguration):
        from tth_opencode.harness.compatibility import match_release

        release = match_release("1.2.27", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("tth_opencode.harness.adapter.probe_opencode", probe_opencode)


def make_adapter_factory(
    kind: HarnessKind,
    monkeypatch: Any,
) -> Callable[[], tuple[HarnessAdapter, Callable[[HarnessAdapter], Any]]]:
    _patch_probe(monkeypatch, kind)

    def factory() -> tuple[HarnessAdapter, Callable[[HarnessAdapter], Any]]:
        adapter = OpenCodeAdapter(http_client_factory=_FakeOpenCodeHttp)
        adapter.prepare_port(18080)
        return adapter, _noop_bind

    return factory


def config_for(kind: HarnessKind) -> HarnessConfiguration:
    return HarnessConfiguration(
        kind=kind, working_directory="/tmp", model="test/default", mode="default"
    )


def _noop_bind(_adapter: HarnessAdapter) -> None:
    return None


def capabilities_for(kind: HarnessKind) -> HarnessCapabilities:
    return HarnessCapabilities(kind=kind, version="test")
