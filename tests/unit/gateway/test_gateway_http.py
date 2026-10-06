import json
import logging
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from tth_types.enums import HarnessKind
from tth_types.sandbox import SandboxPolicy, SandboxPolicyRef, SandboxPolicyRevision

pytest.importorskip("mitmproxy")
from mitmproxy import connection, http

from talktoharnesses.gateway.credentials import atomic_json
from talktoharnesses.gateway.routes import META_GATEWAY_BASE, same_host
from talktoharnesses.gateway.server import GatewayConfig, PolicyGateway

FRONTED = "attacker.example"


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
    # Like any HTTP/1.1 client, name the URL's origin in a Host header.
    headers = {"Host": urlsplit(url).netloc, **headers}
    result.request = http.Request.make(
        method, url, headers=[(key.encode(), value.encode()) for key, value in headers.items()]
    )
    return result


def tunnelled(
    host: str,
    path: str = "/simple/",
    method: str = "GET",
    *,
    authority: str = "",
    http2: bool = False,
    headers: tuple[tuple[str, str], ...] = (),
) -> http.HTTPFlow:
    """A request as mitmproxy's transparent layer hands it on inside a CONNECT tunnel.

    The tunnel supplies ``request.host``; Host headers and any request-target authority
    (absolute-form, or HTTP/2 ``:authority``) arrive exactly as the agent sent them.
    """
    result = http.HTTPFlow(
        connection.Client(peername=("127.0.0.1", 12), sockname=("127.0.0.1", 8080)),
        connection.Server(address=(host, 443)),
    )
    result.request = http.Request(
        host,
        443,
        method.encode(),
        b"https",
        authority.encode(),
        path.encode(),
        b"HTTP/2.0" if http2 else b"HTTP/1.1",
        tuple((name.encode(), value.encode()) for name, value in headers),
        b"",
        None,
        0.0,
        None,
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
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from talktoharnesses.gateway import server

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
    server.logger.addHandler(caplog.handler)
    try:
        proxy.response(request)
    finally:
        server.logger.removeHandler(caplog.handler)
    assert request.response.status_code == 403
    assert "never-return" not in (request.response.text or "")
    assert "rotated" not in (request.response.text or "")
    assert "refresh_lock" not in request.metadata
    # The upstream already rotated the tokens, so the host keeps them anyway.
    assert json.loads((tmp_path / "auth.json").read_text())["tokens"]["access_token"] == "rotated"
    assert "credential_exchange_rejected provider=openai" in caplog.text
    assert "custom_secret" in caplog.text
    assert "never-return" not in caplog.text and "rotated" not in caplog.text


def test_dns_private_results_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import socket

    from talktoharnesses.gateway.server import GatewayEventLoop

    answers = {
        "pypi.org": ["151.101.0.223"],
        "rebound.example": ["151.101.0.223", "169.254.169.254"],
        "gateway.localhost": ["127.0.0.1"],
    }

    def resolve(host: str, port: int, *args: Any) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            for address in answers[host]
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    loop = GatewayEventLoop()
    try:
        admitted = loop.run_until_complete(loop.getaddrinfo("pypi.org", 443))
        assert [item[4] for item in admitted] == [("151.101.0.223", 443)]
        # mitmproxy opens upstream connections this way. One private answer
        # denies the name rather than leaving it to connection order.
        with pytest.raises(OSError, match="Sandbox policy denied this address."):
            loop.run_until_complete(asyncio.open_connection("rebound.example", 443))
        # Binding the gateway's own listening sockets is not an outbound connection.
        listener = loop.run_until_complete(
            asyncio.start_server(lambda reader, writer: None, "gateway.localhost", 0)
        )
        listener.close()
        loop.run_until_complete(listener.wait_closed())
    finally:
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()


def test_serve_runs_the_gateway_on_the_vetting_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from talktoharnesses.gateway import server

    loops: list[type[asyncio.AbstractEventLoop]] = []

    async def run(config: GatewayConfig) -> None:
        loops.append(type(asyncio.get_running_loop()))

    monkeypatch.setattr(server, "_serve", run)
    config = tmp_path / "config.json"
    config.write_text(gateway(tmp_path).config.model_dump_json())
    server.serve(config)
    assert loops == [server.GatewayEventLoop]


def test_split_address_must_be_an_ip_literal(tmp_path: Path) -> None:
    from pydantic import ValidationError

    config = gateway(tmp_path).config.model_dump()
    # Only IP literals bypass the resolver; a name would resolve to a private address.
    for address in ("split.internal", "fe80::1%eth0", ""):
        with pytest.raises(ValidationError):
            GatewayConfig.model_validate({**config, "split_address": address})


def test_admitted_upstream_connection_stays_reusable_for_its_host(tmp_path: Path) -> None:
    import asyncio

    from mitmproxy.proxy.layers.http import GetHttpConnection
    from mitmproxy.proxy.server_hooks import ServerConnectionHookData

    from talktoharnesses.gateway.server import CONNECTION_DENIED, GatewayEventLoop

    proxy = gateway(tmp_path)
    address = ("registry.npmjs.org", 443)
    client = flow("https://registry.npmjs.org/").client_conn

    async def admit() -> connection.Server:
        server = connection.Server(address=address, tls=True)
        proxy.server_connect(ServerConnectionHookData(client=client, server=server))
        return server

    # On any other loop nothing would check the addresses the name resolves to.
    with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
        assert runner.run(admit()).error == CONNECTION_DENIED
    with asyncio.Runner(loop_factory=GatewayEventLoop) as runner:
        server = runner.run(admit())
    assert server.error is None
    assert server.sni == "registry.npmjs.org"
    # mitmproxy hands the tunnel's next request this connection only while its
    # address still matches. Otherwise each request opens another connection.
    assert GetHttpConnection(address, True, None).connection_spec_matches(server)


def test_keep_alive_tunnel_reuses_one_upstream_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """npm holds one tunnel per pooled socket; its sixth request once stalled."""
    import asyncio
    import http.client
    import socket
    import ssl

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from mitmproxy import options
    from mitmproxy.certs import CertStore

    from talktoharnesses.gateway import server

    host = "registry.npmjs.org"
    confdir = tmp_path / "ca"
    ca = confdir / "mitmproxy-ca-cert.pem"
    leaf = CertStore.from_store(confdir, "mitmproxy", 2048).get_cert(host, [x509.DNSName(host)])
    (tmp_path / "leaf.pem").write_bytes(leaf.cert.to_pem())
    (tmp_path / "leaf.key").write_bytes(
        leaf.privatekey.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    handshakes: list[str | None] = []
    registry_tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    registry_tls.load_cert_chain(tmp_path / "leaf.pem", tmp_path / "leaf.key")
    registry_tls.sni_callback = lambda _socket, name, _context: handshakes.append(name)

    async def registry(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while await reader.readuntil(b"\r\n\r\n"):
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\ntarball")
                await writer.drain()
        except (asyncio.IncompleteReadError, OSError):
            writer.close()

    upstream_port = 0
    real_getaddrinfo = socket.getaddrinfo

    def resolve(name: str, port: int, *args: Any) -> list[Any]:
        if name != host:
            return real_getaddrinfo(name, port, *args)
        assert port == 443
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", upstream_port))]

    def loopback_is_public(address: str) -> bool:
        # The local registry listens on loopback; every other address stays private.
        return address == "127.0.0.1"

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(server, "public_address", loopback_is_public)

    def fetch(proxy_port: int) -> list[int]:
        tunnel = http.client.HTTPSConnection(
            "127.0.0.1", proxy_port, timeout=5, context=ssl.create_default_context(cafile=ca)
        )
        tunnel.set_tunnel(host, 443)
        try:
            statuses: list[int] = []
            for index in range(12):
                tunnel.request("GET", f"/package-{index}/-/package-{index}-1.0.0.tgz")
                response = tunnel.getresponse()
                response.read()
                statuses.append(response.status)
            return statuses
        finally:
            tunnel.close()

    async def scenario() -> list[int]:
        nonlocal upstream_port
        upstream = await asyncio.start_server(registry, "127.0.0.1", 0, ssl=registry_tls)
        upstream_port = upstream.sockets[0].getsockname()[1]
        async with upstream:
            master = server.gateway_master(
                gateway(tmp_path).config,
                options.Options(
                    listen_host="127.0.0.1",
                    listen_port=0,
                    confdir=str(confdir),
                    ssl_verify_upstream_trusted_ca=str(ca),
                ),
            )
            running = asyncio.create_task(master.run())
            try:
                proxyserver = cast(Any, master.addons).get("proxyserver")
                async with asyncio.timeout(10):
                    while not proxyserver.listen_addrs():
                        await asyncio.sleep(0.01)
                proxy_port: int = proxyserver.listen_addrs()[0][1]
                return await asyncio.wait_for(asyncio.to_thread(fetch, proxy_port), 30)
            finally:
                master.shutdown()
                await running

    # The production loop, so the connections go through its resolver.
    with asyncio.Runner(loop_factory=server.GatewayEventLoop) as runner:
        assert runner.run(scenario()) == [200] * 12
    assert handshakes == [host]


async def test_denials_log_the_host_but_not_the_path_or_query(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import socket

    from mitmproxy.proxy.server_hooks import ServerConnectionHookData

    from talktoharnesses.gateway import server

    proxy = gateway(tmp_path)
    client = flow("https://pypi.org/").client_conn

    def upstream(address: tuple[str, int], error: str | None = None) -> ServerConnectionHookData:
        data = ServerConnectionHookData(client=client, server=connection.Server(address=address))
        data.server.error = error
        return data

    # What mitmproxy records when GatewayEventLoop refuses the DNS answers.
    private = str(socket.gaierror(socket.EAI_NONAME, server.PRIVATE_ADDRESS_DENIED))
    server.logger.addHandler(caplog.handler)
    try:
        proxy.http_connect(flow("https://unapproved.example/", "CONNECT"))
        proxy.http_connect(flow("https://pypi.org:8443/", "CONNECT"))
        await proxy.requestheaders(flow("https://pypi.org:8443/hidden/path?token=secret"))
        await proxy.requestheaders(flow("http://unapproved.example/hidden"))
        proxy.server_connect(upstream(("unapproved.example", 443)))
        proxy.server_connect(upstream(("pypi.org", 8443)))
        proxy.server_connect_error(upstream(("pypi.org", 443), private))
        proxy.server_connect_error(upstream(("pypi.org", 443), "Connection refused"))
    finally:
        server.logger.removeHandler(caplog.handler)
    messages = [record.getMessage() for record in caplog.records]
    assert [message.split(" reason=")[1] for message in messages] == [
        "egress_denied host=unapproved.example",
        "port_denied host=pypi.org",
        "port_denied host=pypi.org",
        "egress_denied host=unapproved.example",
        "egress_denied host=unapproved.example",
        "port_denied host=pypi.org",
        "private_address host=pypi.org",
    ]
    assert not any("hidden" in message or "secret" in message for message in messages)


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


@pytest.mark.parametrize(
    ("authority", "http2", "headers"),
    [
        # HTTP/1 origin-form: the Host header alone names the virtual host.
        ("", False, (("Host", FRONTED),)),
        # An absolute-form target overrides Host at the origin (RFC 9112, section 3.2.2).
        (FRONTED, False, (("Host", "pypi.org"),)),
        # HTTP/2 :authority, alone or beside a Host header an HTTP/1 upstream would use.
        (FRONTED, True, ()),
        ("pypi.org", True, (("Host", FRONTED),)),
        # A second Host header must not ride behind a matching first one.
        ("", False, (("Host", "pypi.org"), ("Host", FRONTED))),
        ("", False, (("Host", "pypi.org." + FRONTED),)),
        ("", False, (("Host", "pypi.org:8443"),)),
        ("", False, (("Host", ""),)),
    ],
)
async def test_tunnel_host_and_authority_must_name_the_admitted_host(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    authority: str,
    http2: bool,
    headers: tuple[tuple[str, str], ...],
) -> None:
    from talktoharnesses.gateway import server

    # A CDN routes by Host or :authority rather than by the tunnel address or SNI.
    request = tunnelled(
        "pypi.org", "/simple/?q=secret", authority=authority, http2=http2, headers=headers
    )
    monkeypatch.setattr(server.logger, "propagate", True)
    with caplog.at_level(logging.WARNING, logger=server.logger.name):
        await gateway(tmp_path).requestheaders(request)
    assert request.response is not None and request.response.status_code == 403
    assert request.response.json()["error"] == "egress_denied"
    assert "sandbox_policy_denied" in caplog.text
    assert FRONTED not in caplog.text and "simple" not in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.parametrize(
    ("authority", "http2", "headers"),
    [
        ("", False, ()),
        ("", False, (("Host", "PyPI.org"),)),
        ("", False, (("Host", "pypi.org.:443"),)),
        ("pypi.org", False, (("Host", "pypi.org"),)),
        ("pypi.org", True, ()),
        ("pypi.org:443", True, (("Host", "pypi.org"),)),
    ],
)
async def test_tunnel_admits_spellings_of_the_admitted_host(
    tmp_path: Path, authority: str, http2: bool, headers: tuple[tuple[str, str], ...]
) -> None:
    request = tunnelled("pypi.org", authority=authority, http2=http2, headers=headers)
    await gateway(tmp_path).requestheaders(request)
    assert request.response is None


def test_host_comparison_folds_only_ascii_case() -> None:
    assert same_host("KAFKA.example.org.:443", "kafka.example.org")
    assert not same_host("\u212aafka.example.org", "kafka.example.org")


async def test_fronted_provider_request_never_receives_host_credentials(tmp_path: Path) -> None:
    proxy = gateway(tmp_path)
    _, virtual = proxy.vault.snapshot()
    assert virtual is not None
    handle: str = virtual["tokens"]["access_token"]
    request = tunnelled(
        "api.openai.com",
        "/v1/responses",
        "POST",
        headers=(("Host", FRONTED), ("Authorization", "Bearer " + handle)),
    )
    await proxy.requestheaders(request)
    assert request.response is not None and request.response.status_code == 403
    assert request.request.headers["Authorization"] == "Bearer " + handle
    assert "provider_route" not in request.metadata


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
    assert request.request.headers["Host"] == "api.meta.ai"
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
    assert request.request.headers["Host"] == "127.0.0.1:8081"
    assert request.request.stream is True
    assert "Authorization" not in request.request.headers
    assert "Cookie" not in request.request.headers
    assert request.request.headers["Accept"] == "text/event-stream"
    request.server_conn.address = ("127.0.0.1", 8081)
    data = ServerConnectionHookData(client=request.client_conn, server=request.server_conn)
    proxy.server_connect(data)
    assert data.server.error is None
    # The prefix only matters on the gateway's own origin.
    elsewhere = flow("http://pypi.org/__tth/mcp/handle")
    await proxy.requestheaders(elsewhere)
    assert elsewhere.response is not None and elsewhere.response.status_code == 403
    loopback = flow("https://example.org/")
    loopback.server_conn.address = ("127.0.0.1", 8082)
    data = ServerConnectionHookData(client=loopback.client_conn, server=loopback.server_conn)
    proxy.server_connect(data)
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
