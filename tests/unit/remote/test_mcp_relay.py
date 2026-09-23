import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from tth_types.harness import HarnessMcpHeader, HarnessMcpServer

from talktoharnesses.remote import mcp_relay
from talktoharnesses.remote.mcp_relay import (
    ROUTES_FILE,
    SOCKET_FILE,
    McpRelayApp,
    ensure_mcp_relay,
    register_mcp_servers,
)

SECRET = HarnessMcpHeader(name="Authorization", value="Bearer host-secret")


class StreamingTransport(httpx.AsyncBaseTransport):
    """Unlike ``MockTransport``, leaves the response body unread like a real upstream."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        return self.handler(request)


def server(url: str = "http://localhost:8001/mcp/projects/1/memory", **kwargs: object):
    return HarnessMcpServer(name="memory", url=url, headers=(SECRET,), **kwargs)


def test_registration_hands_the_agent_only_opaque_gateway_urls(tmp_path: Path) -> None:
    first, slashed = register_mcp_servers(
        tmp_path,
        "seed",
        (
            server(),
            server("http://localhost:8001/mcp/projects/1/wiki/").model_copy(
                update={"name": "wiki"}
            ),
        ),
    )
    assert first.name == "memory"
    assert first.headers == ()
    assert first.url.startswith("http://tth-gateway.invalid:8080/__tth/mcp/")
    assert not first.url.endswith("/")
    assert slashed.url.endswith("/")
    assert "host-secret" not in first.url + slashed.url
    assert "localhost" not in first.url
    # Stable across registrations; a rotated secret gets a distinct route.
    (again,) = register_mcp_servers(tmp_path, "seed", (server(),))
    assert again.url == first.url
    rotated = server().model_copy(
        update={"headers": (HarnessMcpHeader(name="Authorization", value="Bearer next"),)}
    )
    (other,) = register_mcp_servers(tmp_path, "seed", (rotated,))
    assert other.url != first.url
    routes = json.loads((tmp_path / ROUTES_FILE).read_text())
    assert len(routes) == 3
    assert (tmp_path / ROUTES_FILE).stat().st_mode & 0o777 == 0o600
    assert register_mcp_servers(tmp_path, "seed", ()) == ()


async def test_relay_restores_the_upstream_and_its_headers(tmp_path: Path) -> None:
    (virtual,) = register_mcp_servers(tmp_path, "seed", (server(),))
    handle_path = httpx.URL(virtual.url).path
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b'data: {"ok": true}\n\n'),
            headers={"Content-Type": "text/event-stream", "Set-Cookie": "upstream=1"},
        )

    app = McpRelayApp(tmp_path, transport=StreamingTransport(upstream))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay"
    ) as client:
        response = await client.post(
            handle_path,
            content=b'{"jsonrpc":"2.0"}',
            headers={"Authorization": "Bearer forged", "Mcp-Session-Id": "abc"},
        )
        assert response.status_code == 200
        assert response.text == 'data: {"ok": true}\n\n'
        assert "set-cookie" not in response.headers
        await client.get(handle_path + "/sub", params={"cursor": "1"})
        unknown = await client.post("/__tth/mcp/unknown", content=b"{}")
        assert unknown.status_code == 404
        outside = await client.get("/elsewhere")
        assert outside.status_code == 404
    first, second = seen
    assert str(first.url) == "http://localhost:8001/mcp/projects/1/memory"
    assert first.headers.get_list("Authorization") == ["Bearer host-secret"]
    assert first.headers["Mcp-Session-Id"] == "abc"
    assert first.content == b'{"jsonrpc":"2.0"}'
    assert str(second.url) == "http://localhost:8001/mcp/projects/1/memory/sub?cursor=1"


async def test_unreachable_upstream_is_a_gateway_error(tmp_path: Path) -> None:
    (virtual,) = register_mcp_servers(tmp_path, "seed", (server(),))

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    app = McpRelayApp(tmp_path, transport=StreamingTransport(refuse))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay"
    ) as client:
        response = await client.post(httpx.URL(virtual.url).path, content=b"{}")
    assert response.status_code == 502
    assert response.json() == {"error": "mcp_upstream_unavailable"}


async def test_relay_serves_the_sandbox_socket_once_per_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_relay, "_relays", {})
    # Sandbox state paths can exceed the Unix socket address limit.
    state = tmp_path / ("s" * 90)
    state.mkdir()
    ensure_mcp_relay(state)
    thread = mcp_relay._relays[state.resolve()]  # pyright: ignore[reportPrivateUsage]
    ensure_mcp_relay(state)
    assert mcp_relay._relays[state.resolve()] is thread  # pyright: ignore[reportPrivateUsage]
    socket_path = state / SOCKET_FILE
    assert socket_path.stat().st_mode & 0o777 == 0o600
    directory = os.open(state, os.O_RDONLY)
    try:
        transport = httpx.AsyncHTTPTransport(uds=f"/proc/self/fd/{directory}/{SOCKET_FILE}")
        async with httpx.AsyncClient(transport=transport, base_url="http://relay") as client:
            for _ in range(50):
                try:
                    response = await client.get("/__tth/mcp/unknown")
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.05)
            else:
                pytest.fail("relay did not start")
    finally:
        os.close(directory)
    assert response.status_code == 404
