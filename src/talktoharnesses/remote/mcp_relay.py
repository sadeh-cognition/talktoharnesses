"""Host-side relay that lets a policy sandbox reach MCP servers without their secrets.

A managed sandbox's only exit is its gateway, which cannot reach the proxy
host's loopback interface and must never hold MCP credentials in the agent's
view. Each MCP server a split is given is replaced by an opaque gateway URL.
The gateway forwards those requests over a Unix socket in its private state
directory to this relay, which runs on the proxy host, restores the server's
real URL and headers, and streams the response back.

Routes are persisted in the sandbox state directory so a relay restarted with
the proxy serves splits that were configured by an earlier process.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import json
import os
import socket
import threading
from collections.abc import Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict
from tth_types.harness import HarnessMcpHeader, HarnessMcpServer

from talktoharnesses.gateway.credentials import atomic_json
from talktoharnesses.gateway.routes import GATEWAY_HOST, GATEWAY_PORT, MCP_ROUTE_PREFIX

ROUTES_FILE = "mcp-routes.json"
SOCKET_FILE = "mcp.sock"

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

# Connection-scoped and sandbox-supplied credential headers never cross the relay.
_DROPPED_REQUEST_HEADERS = frozenset(
    {
        "host",
        "connection",
        "keep-alive",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "authorization",
        "cookie",
        "x-api-key",
        "api-key",
    }
)
_DROPPED_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "set-cookie",
    }
)


class McpRoute(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    headers: tuple[HarnessMcpHeader, ...] = ()


def mcp_handle(seed: str, server: HarnessMcpServer) -> str:
    """A stable, externally meaningless identifier for one upstream and its headers."""
    payload = json.dumps(
        [server.url, [[header.name, header.value] for header in server.headers]]
    ).encode()
    return hmac.new(seed.encode(), payload, hashlib.sha256).hexdigest()[:32]


def virtual_url(handle: str, upstream: str) -> str:
    suffix = "/" if urlsplit(upstream).path.endswith("/") else ""
    return f"http://{GATEWAY_HOST}:{GATEWAY_PORT}{MCP_ROUTE_PREFIX}{handle}{suffix}"


def register_mcp_servers(
    state: Path, seed: str, servers: tuple[HarnessMcpServer, ...]
) -> tuple[HarnessMcpServer, ...]:
    """Record ``servers`` for this sandbox and return their credential-free gateway form."""
    if not servers:
        return servers
    routes = {mcp_handle(seed, server): server for server in servers}
    path = state / ROUTES_FILE
    with (state / "mcp-routes.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        stored: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}
        updated = {
            **stored,
            **{
                handle: McpRoute(url=server.url, headers=server.headers).model_dump(mode="json")
                for handle, server in routes.items()
            },
        }
        if updated != stored:
            atomic_json(path, updated)
    return tuple(
        server.model_copy(update={"url": virtual_url(handle, server.url), "headers": ()})
        for handle, server in routes.items()
    )


class McpRelayApp:
    """ASGI app serving one sandbox's registered MCP routes."""

    def __init__(self, state: Path, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.state = state
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _route(self, handle: str) -> McpRoute | None:
        path = self.state / ROUTES_FILE
        if not path.exists():
            return None
        raw = json.loads(path.read_text()).get(handle)
        return None if raw is None else McpRoute.model_validate(raw)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                # MCP responses may be long-lived event streams.
                timeout=httpx.Timeout(connect=10, read=None, write=30, pool=10),
                follow_redirects=False,
            )
        return self._client

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        path: str = scope["path"]
        handle, _, rest = path.removeprefix(MCP_ROUTE_PREFIX).partition("/")
        route = self._route(handle) if path.startswith(MCP_ROUTE_PREFIX) else None
        if route is None:
            await _respond(send, 404, {"error": "mcp_route_unknown"})
            return
        upstream = route.url.rstrip("/") + "/" + rest if rest or path.endswith("/") else route.url
        query: bytes = scope.get("query_string", b"")
        if query:
            upstream += "?" + query.decode("latin-1")
        headers = [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in scope["headers"]
            if name.decode("latin-1").lower() not in _DROPPED_REQUEST_HEADERS
        ]
        headers.extend((header.name, header.value) for header in route.headers)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if not message.get("more_body"):
                break
        request = self._http().build_request(
            scope["method"], upstream, headers=headers, content=bytes(body)
        )
        try:
            response = await self._http().send(request, stream=True)
        except httpx.HTTPError:
            await _respond(send, 502, {"error": "mcp_upstream_unavailable"})
            return
        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": response.status_code,
                    "headers": [
                        (name.encode("latin-1"), value.encode("latin-1"))
                        for name, value in response.headers.multi_items()
                        if name.lower() not in _DROPPED_RESPONSE_HEADERS
                    ],
                }
            )
            async for chunk in response.aiter_raw():
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b""})
        finally:
            await response.aclose()


async def _respond(send: Send, status: int, body: dict[str, str]) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": json.dumps(body).encode()})


def _bind(path: Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path.unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        # Sandbox state paths can exceed the Unix socket path limit; binding
        # through the directory descriptor keeps the address short.
        target = str(path) if len(str(path)) < 100 else f"/proc/self/fd/{directory}/{path.name}"
        sock.bind(target)
    finally:
        os.close(directory)
    path.chmod(0o600)
    sock.listen(128)
    return sock


_relays: dict[Path, threading.Thread] = {}
_relays_lock = threading.Lock()


def ensure_mcp_relay(state: Path) -> None:
    """Serve ``state``'s MCP routes from this process, idempotently."""
    import uvicorn

    state = state.resolve()
    with _relays_lock:
        running = _relays.get(state)
        if running is not None and running.is_alive():
            return
        sock = _bind(state / SOCKET_FILE)
        server = uvicorn.Server(
            uvicorn.Config(
                McpRelayApp(state),
                lifespan="off",
                access_log=False,
                log_level="warning",
                timeout_keep_alive=30,
            )
        )
        thread = threading.Thread(
            target=lambda: asyncio.run(server.serve(sockets=[sock])),
            name=f"tth-mcp-relay-{state.name}",
            daemon=True,
        )
        thread.start()
        _relays[state] = thread
