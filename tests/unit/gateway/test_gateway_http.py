import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from tth_types.enums import HarnessKind
from tth_types.sandbox import SandboxPolicy, SandboxPolicyRef, SandboxPolicyRevision

pytest.importorskip("mitmproxy")
from mitmproxy import connection, http

from talktoharnesses.gateway.credentials import atomic_json
from talktoharnesses.gateway.routes import META_GATEWAY_BASE
from talktoharnesses.gateway.server import GatewayConfig, PolicyGateway


def gateway(tmp_path: Path) -> PolicyGateway:
    auth = tmp_path / "auth.json"
    atomic_json(auth, {"tokens": {"access_token": "real-access", "refresh_token": "real-refresh"}})
    return PolicyGateway(
        GatewayConfig(
            revision=SandboxPolicyRevision(
                ref=SandboxPolicyRef(id=uuid4(), revision=1),
                policy=SandboxPolicy(project_root="/repo"),
            ),
            kind=HarnessKind.CODEX,
            seed="scope",
            control_token="host-only",
            split_token="split-only",
            split_address="172.20.0.2",
            auth_file=str(auth),
        )
    )


def flow(url: str, method: str = "GET", **headers: str) -> http.HTTPFlow:
    result = http.HTTPFlow(
        connection.Client(peername=("127.0.0.1", 12), sockname=("127.0.0.1", 8080)),
        connection.Server(address=None),
    )
    result.request = http.Request.make(
        method, url, headers=[(key.encode(), value.encode()) for key, value in headers.items()]
    )
    return result


async def test_credentials_only_substituted_for_provider_authentication(tmp_path: Path) -> None:
    proxy = gateway(tmp_path)
    _, virtual = proxy.vault.snapshot()
    assert virtual is not None
    handle: str = virtual["tokens"]["access_token"]
    request = flow("https://api.openai.com/v1/responses", "POST", Authorization="Bearer " + handle)
    request.request.text = '{"input":"' + handle + '"}'
    await proxy.requestheaders(request)
    proxy.request(request)
    assert request.response is None
    assert request.request.stream is True
    assert request.request.headers["Authorization"] == "Bearer real-access"
    assert "real-access" not in (request.request.text or "")
    package = flow("https://pypi.org/simple/", Authorization="Bearer " + handle)
    await proxy.requestheaders(package)
    assert package.response and package.response.status_code == 403
    wrong_endpoint = flow(
        "https://api.openai.com/v1/organization/api_keys", Authorization="Bearer " + handle
    )
    await proxy.requestheaders(wrong_endpoint)
    assert wrong_endpoint.response and wrong_endpoint.response.status_code == 403


async def test_control_route_requires_distinct_host_token(tmp_path: Path) -> None:
    proxy = gateway(tmp_path)
    for token in ("", "split-only"):
        request = flow(
            "http://tth-gateway.invalid/split/v1/sessions", "POST", **{"X-TTH-Split-Token": token}
        )
        await proxy.requestheaders(request)
        assert request.response and request.response.status_code == 403
    admitted = flow(
        "http://127.0.0.1/split/v1/sessions", "POST", **{"X-TTH-Split-Token": "host-only"}
    )
    await proxy.requestheaders(admitted)
    assert admitted.response is None
    assert admitted.request.host == "172.20.0.2"
    assert admitted.request.headers["X-TTH-Split-Token"] == "split-only"


async def test_refresh_response_never_returns_tokens_or_unknown_secret_fields(
    tmp_path: Path,
) -> None:
    proxy = gateway(tmp_path)
    _, virtual = proxy.vault.snapshot()
    assert virtual is not None
    request = flow("https://auth.openai.com/oauth/token", "POST")
    request.request.text = '{"refresh_token":"' + virtual["tokens"]["refresh_token"] + '"}'
    await proxy.requestheaders(request)
    proxy.request(request)
    assert "real-refresh" in (request.request.text or "")
    request.response = http.Response.make(
        200, '{"access_token":"rotated","custom_secret":"never-return"}'
    )
    proxy.responseheaders(request)
    proxy.response(request)
    assert request.response.status_code == 403
    assert "never-return" not in (request.response.text or "")
    assert "rotated" not in (request.response.text or "")
    assert "refresh_lock" not in request.metadata


async def test_dns_private_results_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import socket

    from mitmproxy.proxy.server_hooks import ServerConnectionHookData

    async def resolve(*args: Any, **kwargs: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    proxy = gateway(tmp_path)
    request = flow("https://pypi.org/simple/")
    request.server_conn.address = ("pypi.org", 443)
    data = ServerConnectionHookData(client=request.client_conn, server=request.server_conn)
    await proxy.server_connect(data)
    assert data.server.error == "Sandbox policy denied this address."


async def test_split_event_stream_is_not_buffered(tmp_path: Path) -> None:
    proxy = gateway(tmp_path)
    request = flow(
        "http://127.0.0.1/split/v1/sessions/id/events", **{"X-TTH-Split-Token": "host-only"}
    )
    await proxy.requestheaders(request)
    request.response = http.Response.make(200, b"", {"Content-Type": "text/event-stream"})
    proxy.responseheaders(request)
    assert request.response.stream is True


async def test_command_endpoint_and_connect_fail_closed(tmp_path: Path) -> None:
    proxy = gateway(tmp_path)
    for url in (
        "https://169.254.169.254/",
        "https://pypi.org:8443/",
        "https://unapproved.example/",
    ):
        request = flow(url, "CONNECT")
        proxy.http_connect(request)
        assert request.response is not None and request.response.status_code == 403
    request = flow("http://tth-gateway.invalid/__tth/command-check", "POST")
    request.request.text = '{"command":"env git -C /repo push","cwd":"/repo"}'
    await proxy.requestheaders(request)
    proxy.request(request)
    assert request.response is not None
    assert request.response.json()["allowed"] is False
    request.response = None
    request.request.text = "invalid JSON"
    proxy.request(request)
    assert request.response is not None and request.response.status_code == 403


async def test_successful_refresh_returns_handles_and_clears_secret_headers(tmp_path: Path) -> None:
    proxy = gateway(tmp_path)
    _, virtual = proxy.vault.snapshot()
    assert virtual is not None
    request = flow(
        "https://auth.openai.com/oauth/token",
        "POST",
        **{"Content-Type": "application/x-www-form-urlencoded"},
    )
    request.request.text = "refresh_token=" + virtual["tokens"]["refresh_token"]
    await proxy.requestheaders(request)
    proxy.request(request)
    assert request.request.text == "refresh_token=real-refresh"
    assert request.request.stream is False
    request.response = http.Response.make(
        200, '{"access_token":"new-secret"}', {"Set-Cookie": "never-return"}
    )
    proxy.responseheaders(request)
    proxy.response(request)
    assert request.response.status_code == 200
    assert "new-secret" not in (request.response.text or "")
    assert "Set-Cookie" not in request.response.headers
    assert "refresh_lock" not in request.metadata
    assert proxy.vault.substitute(request.response.json()["access_token"], "openai") == "new-secret"


@pytest.mark.parametrize("existing_login", [False, True])
async def test_cursor_api_key_login_persists_only_host_tokens_and_survives_restart(
    tmp_path: Path, existing_login: bool
) -> None:
    config = gateway(tmp_path).config.model_copy(
        update={
            "kind": HarnessKind.CURSOR,
            "api_keys": {"CURSOR_API_KEY": "host-api-key"},
            "auth_file": str(tmp_path / "auth.json") if existing_login else None,
            "cursor_login_file": str(tmp_path / "cursor-login.json"),
        }
    )
    original = (tmp_path / "auth.json").read_text()
    old_handle = ""
    for rotation in range(2):
        proxy = PolicyGateway(config)
        env, _ = proxy.vault.snapshot()
        request = flow(
            "https://api2.cursor.sh/auth/exchange_user_api_key",
            "POST",
            Authorization="Bearer " + env["CURSOR_API_KEY"],
        )
        request.request.text = "{}"
        await proxy.requestheaders(request)
        proxy.request(request)
        assert request.response is None
        assert request.request.headers["Authorization"] == "Bearer host-api-key"
        assert request.request.stream is False
        request.response = http.Response.make(
            200,
            json.dumps(
                {
                    "accessToken": f"host-access-{rotation}",
                    "refreshToken": f"host-refresh-{rotation}",
                    "unknownSecret": "must-not-reach-agent",
                }
            ),
        )
        proxy.responseheaders(request)
        proxy.response(request)
        assert request.response.status_code == 200
        handles = request.response.json()
        assert set(handles) == {"accessToken", "refreshToken"}
        body = request.response.text
        assert body is not None
        assert "host-" not in body and "must-not-reach-agent" not in body
        assert "refresh_lock" not in request.metadata
        path = Path(config.cursor_login_file)
        assert path.stat().st_mode & 0o777 == 0o600
        assert json.loads(path.read_text())["accessToken"] == f"host-access-{rotation}"
        # A new gateway process must resolve the handles saved by the native CLI.
        restarted = PolicyGateway(config)
        inference = flow(
            "https://api2.cursor.sh/agent.v1.AgentService/Run",
            "POST",
            Authorization="Bearer " + (old_handle or handles["accessToken"]),
        )
        await restarted.requestheaders(inference)
        assert inference.response is None
        assert inference.request.headers["Authorization"] == f"Bearer host-access-{rotation}"
        old_handle = handles["accessToken"]
    assert (tmp_path / "auth.json").read_text() == original


async def test_muse_reverse_proxy_uses_fixed_tls_origin_and_the_same_route_policy(
    tmp_path: Path,
) -> None:
    auth = tmp_path / "muse.json"
    atomic_json(
        auth,
        {
            "schema_version": 1,
            "providers": {
                "meta": {
                    "mechanism": "oauth",
                    "access_token": "host-only-oauth",
                    "api_key": "host-only-key",
                    "api_base_url": "https://api.meta.ai/v1",
                }
            },
        },
    )
    proxy = PolicyGateway(
        gateway(tmp_path).config.model_copy(
            update={
                "kind": HarnessKind.MUSE,
                "auth_file": str(auth),
            }
        )
    )
    _, virtual = proxy.vault.snapshot()
    assert virtual is not None
    meta = virtual["providers"]["meta"]
    assert meta["api_base_url"] == META_GATEWAY_BASE
    assert "host-only" not in str(virtual)
    request = flow(
        META_GATEWAY_BASE + "/responses", "POST", Authorization="Bearer " + meta["access_token"]
    )
    await proxy.requestheaders(request)
    assert request.response is None
    assert request.request.scheme == "https"
    assert request.request.host == "api.meta.ai" and request.request.port == 443
    assert request.request.headers["Authorization"] == "Bearer host-only-oauth"
    rejected = flow(
        META_GATEWAY_BASE + "/api-keys", "POST", Authorization="Bearer " + meta["access_token"]
    )
    await proxy.requestheaders(rejected)
    assert rejected.response is not None and rejected.response.status_code == 403
    wrong_kind = flow(META_GATEWAY_BASE + "/models")
    await gateway(tmp_path).requestheaders(wrong_kind)
    assert wrong_kind.response is not None and wrong_kind.response.status_code == 403


async def test_mcp_route_reaches_only_the_host_relay_without_agent_credentials(
    tmp_path: Path,
) -> None:
    from mitmproxy.proxy.server_hooks import ServerConnectionHookData

    proxy = gateway(tmp_path)
    request = flow(
        "http://tth-gateway.invalid:8080/__tth/mcp/handle/sub?cursor=1",
        "POST",
        Authorization="Bearer forged",
        Cookie="session=forged",
        Accept="text/event-stream",
    )
    await proxy.requestheaders(request)
    assert request.response is None
    assert (request.request.scheme, request.request.host, request.request.port) == (
        "http",
        "127.0.0.1",
        8081,
    )
    assert request.request.path == "/__tth/mcp/handle/sub?cursor=1"
    assert request.request.stream is True
    assert "Authorization" not in request.request.headers
    assert "Cookie" not in request.request.headers
    assert request.request.headers["Accept"] == "text/event-stream"
    request.server_conn.address = ("127.0.0.1", 8081)
    data = ServerConnectionHookData(client=request.client_conn, server=request.server_conn)
    await proxy.server_connect(data)
    assert data.server.error is None
    # The prefix only matters on the gateway's own origin.
    elsewhere = flow("http://pypi.org/__tth/mcp/handle")
    await proxy.requestheaders(elsewhere)
    assert elsewhere.response is not None and elsewhere.response.status_code == 403
    loopback = flow("https://example.org/")
    loopback.server_conn.address = ("127.0.0.1", 8082)
    data = ServerConnectionHookData(client=loopback.client_conn, server=loopback.server_conn)
    await proxy.server_connect(data)
    assert data.server.error == "Sandbox policy denied this connection."


async def test_mcp_bridge_pipes_loopback_connections_to_the_relay_socket(
    tmp_path: Path,
) -> None:
    import asyncio

    from talktoharnesses.gateway.server import bridge_mcp_relay

    socket_path = str(tmp_path / "relay.sock")

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"relay:" + await reader.readexactly(4))
        await writer.drain()
        writer.close()

    relay = await asyncio.start_unix_server(echo, socket_path)
    bridge = await asyncio.start_server(
        lambda reader, writer: bridge_mcp_relay(reader, writer, socket_path), "127.0.0.1", 0
    )
    async with relay, bridge:
        port = bridge.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"ping")
        await writer.drain()
        assert await reader.read() == b"relay:ping"
        writer.close()
        # A missing relay closes the agent's connection instead of hanging.
        missing = await asyncio.start_server(
            lambda r, w: bridge_mcp_relay(r, w, str(tmp_path / "absent.sock")), "127.0.0.1", 0
        )
        async with missing:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", missing.sockets[0].getsockname()[1]
            )
            assert await reader.read() == b""
            writer.close()
