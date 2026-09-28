"""HTTPS gateway process. Never run this process inside an agent container."""

from __future__ import annotations

import asyncio
import fcntl
import hmac
import ipaddress
import json
import logging
import socket
import sys
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlencode

from mitmproxy import http, options
from mitmproxy.proxy import server_hooks
from mitmproxy.tools.dump import DumpMaster
from pydantic import BaseModel, Field, field_validator
from tth_types.enums import HarnessKind
from tth_types.sandbox import CommandCheck, SandboxPolicyRevision

from talktoharnesses.command_policy import check_command
from talktoharnesses.gateway.credentials import CredentialVault
from talktoharnesses.gateway.routes import (
    GATEWAY_HOST,
    GATEWAY_PORT,
    KIND_PROVIDERS,
    MCP_RELAY_PORT,
    MCP_RELAY_SOCKET,
    MCP_ROUTE_PREFIX,
    META_API_HOST,
    PROVIDER_ROUTES,
    ProviderRoute,
    TokenExchange,
    normalized_path,
    permitted_request,
    provider_route,
    public_address,
    same_host,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.WARNING)
logger.propagate = False

CONNECTION_DENIED = "Sandbox policy denied this connection."
PRIVATE_ADDRESS_DENIED = "Sandbox policy denied this address."


class GatewayConfig(BaseModel):
    revision: SandboxPolicyRevision
    kind: HarnessKind
    seed: str = Field(repr=False)
    control_token: str = Field(repr=False)
    split_token: str = Field(repr=False)
    split_address: str
    auth_file: str | None = None
    # Host bind identity used during reconciliation; the gateway opens auth_file.
    auth_source: str | None = None
    api_keys: dict[str, str] = Field(default_factory=dict, repr=False)
    cursor_login_file: str = "/state/cursor-login.json"

    @field_validator("split_address")
    @classmethod
    def split_ip_literal(cls, value: str) -> str:
        # The split route skips the public-address check only because
        # GatewayEventLoop never resolves an IP literal.
        if "%" in value:
            raise ValueError("The split address must be an IP address without a zone.")
        ipaddress.ip_address(value)
        return value


class PolicyGateway:
    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self.admitted_hosts = frozenset(
            {rule.host for rule in config.revision.policy.egress}
            | {
                route.rule.host
                for route in PROVIDER_ROUTES
                if route.provider in KIND_PROVIDERS[config.kind]
            }
        )
        self.vault = CredentialVault(
            kind=config.kind,
            seed=config.seed,
            auth_file=Path(config.auth_file) if config.auth_file else None,
            api_keys=config.api_keys,
            cursor_login_file=Path(config.cursor_login_file)
            if config.kind is HarnessKind.CURSOR
            else None,
        )

    def _log_denial(self, reason: str, host: str) -> None:
        # Record the host so denials can be diagnosed. Do not record paths, query
        # strings, bodies or headers. They can contain credentials or arbitrary
        # text even when a request was denied.
        logger.warning(
            "sandbox_policy_denied policy=%s revision=%s reason=%s host=%s",
            self.config.revision.ref.id,
            self.config.revision.ref.revision,
            reason,
            host,
        )

    def _deny(self, flow: http.HTTPFlow, reason: str = "egress_denied") -> None:
        self._log_denial(reason, flow.request.host)
        flow.response = http.Response.make(
            403,
            json.dumps(
                {
                    "error": reason,
                    "policy": str(self.config.revision.ref.id),
                    "revision": self.config.revision.ref.revision,
                }
            ).encode(),
            {"Content-Type": "application/json"},
        )

    def http_connect(self, flow: http.HTTPFlow) -> None:
        if flow.request.host.lower().rstrip(".") not in self.admitted_hosts:
            self._deny(flow)
        elif flow.request.port != 443:
            self._deny(flow, "port_denied")

    async def _refresh_lock(self, flow: http.HTTPFlow, exchange: TokenExchange) -> None:
        stream = self.vault.exchange_file(exchange).with_suffix(".tth-refresh.lock").open("a")
        try:
            async with asyncio.timeout(30):
                while True:
                    try:
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(0.05)
            flow.metadata["refresh_lock"] = stream
        except BaseException:
            stream.close()
            raise

    @staticmethod
    def _unlock(flow: http.HTTPFlow) -> None:
        if stream := flow.metadata.pop("refresh_lock", None):
            stream.close()

    async def requestheaders(self, flow: http.HTTPFlow) -> None:
        request = flow.request
        path = normalized_path(request.path)
        if path is None:
            self._deny(flow)
            return
        if (
            self.config.kind is HarnessKind.MUSE
            and request.host == GATEWAY_HOST
            and request.port == GATEWAY_PORT
            and provider_route(self.config.kind, META_API_HOST, path, request.method) is not None
        ):
            request.scheme = "https"
            request.host = META_API_HOST
            request.port = 443
            cast(Any, request.headers)["Host"] = META_API_HOST
        if path.startswith("/split/"):
            health = path == "/split/v1/health" and request.method == "GET"
            token = cast(Any, request.headers).pop("X-TTH-Split-Token", "")
            if not health and not hmac.compare_digest(token, self.config.control_token):
                self._deny(flow, "control_access_denied")
                return
            request.scheme = "http"
            request.host = self.config.split_address
            request.port = 8010
            request.path = request.path.removeprefix("/split")
            cast(Any, request.headers)["Host"] = f"{request.host}:8010"
            cast(Any, request.headers)["X-TTH-Split-Token"] = self.config.split_token
            flow.metadata["control"] = True
            return
        if (
            request.host == GATEWAY_HOST
            and path == "/__tth/command-check"
            and request.method == "POST"
        ):
            return
        if (
            request.host == GATEWAY_HOST
            and request.port == GATEWAY_PORT
            and path.startswith(MCP_ROUTE_PREFIX)
        ):
            # The host relay adds the server's real headers; anything the
            # agent supplies is discarded rather than forwarded.
            for name in ("Authorization", "Cookie", "X-Api-Key", "Api-Key", "Proxy-Authorization"):
                cast(Any, request.headers).pop(name, None)
            request.scheme = "http"
            request.host = "127.0.0.1"
            request.port = MCP_RELAY_PORT
            cast(Any, request.headers)["Host"] = f"127.0.0.1:{MCP_RELAY_PORT}"
            request.stream = True
            return
        if request.scheme != "https" or request.port != 443:
            admitted = request.host.lower().rstrip(".") in self.admitted_hosts
            self._deny(flow, "port_denied" if admitted else "egress_denied")
            return
        # Inside a CONNECT tunnel request.host is the tunnel's address, but the
        # agent's Host headers and request-target authority (absolute-form or
        # HTTP/2 :authority) are forwarded unchanged. A CDN routing by those would
        # reach a tenant the policy never admitted (domain fronting).
        names: list[str] = cast(Any, request.headers).get_all("Host")
        if request.authority:
            names.append(request.authority)
        if not all(same_host(name, request.host) for name in names):
            self._deny(flow)
            return
        route = provider_route(self.config.kind, request.host, path, request.method)
        if route is None and not permitted_request(
            self.config.revision.policy, request.host, request.path, request.method
        ):
            self._deny(flow)
            return
        try:
            if route is not None:
                flow.metadata["provider_route"] = route
                if route.exchange is not None:
                    await self._refresh_lock(flow, route.exchange)
                self.vault.snapshot()
                for name in ("Authorization", "X-Api-Key", "Api-Key", "X-Goog-Api-Key"):
                    if name in cast(Any, request.headers):
                        cast(Any, request.headers)[name] = self.vault.authentication_header(
                            cast(Any, request.headers)[name], route.provider
                        )
                if "Cookie" in cast(Any, request.headers):
                    # Native cookie authentication is only admitted when the
                    # entire cookie field is represented by a credential handle.
                    cast(Any, request.headers)["Cookie"] = self.vault.substitute(
                        cast(Any, request.headers)["Cookie"], route.provider
                    )
                # Native RPC transports can send prompts and tool responses on
                # a bidirectional stream. Only token exchanges need buffering.
                request.stream = route.exchange is None
            else:
                for name in ("Authorization", "Cookie", "X-Api-Key", "Api-Key"):
                    if name in cast(Any, request.headers):
                        self._deny(flow, "service_credentials_unsupported")
                        return
            cast(Any, request.headers).pop("Proxy-Authorization", None)
        except (OSError, ValueError, TimeoutError):
            self._unlock(flow)
            self._deny(flow, "credential_or_gateway_unavailable")

    def request(self, flow: http.HTTPFlow) -> None:
        if flow.response is not None or flow.metadata.get("control"):
            return
        request = flow.request
        if (
            request.host == GATEWAY_HOST
            and request.path == "/__tth/command-check"
            and request.method == "POST"
        ):
            try:
                check = CommandCheck.model_validate_json(request.raw_content or b"")
                decision = check_command(check, self.config.revision.policy.command_rules)
                flow.response = http.Response.make(
                    200, decision.model_dump_json(), {"Content-Type": "application/json"}
                )
            except ValueError:
                self._deny(flow, "invalid_command_check")
            return
        route: ProviderRoute | None = flow.metadata.get("provider_route")
        if route is None or route.exchange is None:
            return
        try:
            if "application/x-www-form-urlencoded" in cast(Any, request.headers).get(
                "Content-Type", ""
            ):
                document = {key: values[0] for key, values in parse_qs(request.get_text()).items()}
                request.text = urlencode(self.vault.refresh_request(document, route.provider))
            else:
                document = json.loads(request.get_text() or "")
                request.text = json.dumps(self.vault.refresh_request(document, route.provider))
        except (ValueError, UnicodeError):
            self._unlock(flow)
            self._deny(flow, "credential_proxy_unsupported")

    def server_connect(self, data: server_hooks.ServerConnectionHookData) -> None:
        address = data.server.address
        if address in ((self.config.split_address, 8010), ("127.0.0.1", MCP_RELAY_PORT)):
            return
        if address is None or address[0].lower().rstrip(".") not in self.admitted_hosts:
            reason = "egress_denied"
        elif address[1] != 443:
            reason = "port_denied"
        elif data.server.transport_protocol != "tcp":
            # Only TCP connections resolve through GatewayEventLoop.
            reason = "egress_denied"
        elif not isinstance(asyncio.get_running_loop(), GatewayEventLoop):
            # Nothing would check the addresses this name resolves to.
            reason = "resolver_unavailable"
        else:
            # Keep the host name as the address. mitmproxy reuses an upstream
            # connection only while its address equals the next request's host
            # and port; a pinned IP here made every request open another
            # connection until mitmproxy's five-per-address limit stalled the
            # keep-alive tunnel. GatewayEventLoop resolves the name to public
            # addresses only.
            data.server.sni = address[0]
            return
        self._log_denial(reason, address[0] if address else "")
        data.server.error = CONNECTION_DENIED

    def server_connect_error(self, data: server_hooks.ServerConnectionHookData) -> None:
        # GatewayEventLoop refused the name's DNS answers; mitmproxy passes on
        # only the error text.
        address = data.server.address
        if address and (data.server.error or "").endswith(PRIVATE_ADDRESS_DENIED):
            self._log_denial("private_address", address[0])

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        if flow.response is None:
            return
        if flow.metadata.get("control"):
            flow.response.stream = True
            return
        for name in ("Set-Cookie", "Authorization", "X-Api-Key"):
            cast(Any, flow.response.headers).pop(name, None)
        route: ProviderRoute | None = flow.metadata.get("provider_route")
        # Inference and package downloads stream; token exchanges are buffered
        # so no credential-bearing response byte reaches the sandbox unchecked.
        if flow.response.status_code < 400 and (route is None or route.exchange is None):
            flow.response.stream = True

    def response(self, flow: http.HTTPFlow) -> None:
        if flow.response is None or flow.metadata.get("control"):
            return
        route: ProviderRoute | None = flow.metadata.get("provider_route")
        try:
            if route is not None and flow.response.status_code >= 400:
                flow.response.content = json.dumps(
                    {"error": "provider_request_failed", "status": flow.response.status_code}
                ).encode()
                cast(Any, flow.response.headers)["Content-Type"] = "application/json"
            elif route is not None and route.exchange is not None:
                document = json.loads(flow.response.get_text() or "")
                flow.response.text = json.dumps(
                    self.vault.refreshed(document, route.provider, exchange=route.exchange)
                )
        except (OSError, ValueError):
            self._deny(flow, "credential_proxy_unsupported")
        finally:
            self._unlock(flow)

    def error(self, flow: http.HTTPFlow) -> None:
        self._unlock(flow)


async def bridge_mcp_relay(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, socket_path: str
) -> None:
    """Pipe one loopback connection to the host MCP relay's Unix socket."""
    try:
        upstream_reader, upstream_writer = await asyncio.open_unix_connection(socket_path)
    except OSError:
        writer.close()
        return

    async def pipe(source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
        try:
            while chunk := await source.read(65536):
                target.write(chunk)
                await target.drain()
        except OSError:
            pass
        finally:
            target.close()

    await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))


class GatewayEventLoop(asyncio.SelectorEventLoop):
    """Event loop whose outbound name resolution only yields public addresses.

    mitmproxy opens every TCP upstream connection through this loop's
    ``getaddrinfo`` and tries only the addresses it returns, so the addresses
    checked here are the ones connected to; ``server_connect`` refuses other
    transports. IP literals are not resolved, and ``server_connect`` admits
    none besides the split and MCP relay addresses. Passive lookups only bind
    the gateway's own listening sockets and are left alone.
    """

    async def getaddrinfo(
        self,
        host: bytes | str | None,
        port: bytes | str | int | None,
        *,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> list[
        tuple[
            socket.AddressFamily,
            socket.SocketKind,
            int,
            str,
            tuple[str, int] | tuple[str, int, int, int] | tuple[int, bytes],
        ]
    ]:
        addresses = await super().getaddrinfo(
            host, port, family=family, type=type, proto=proto, flags=flags
        )
        if flags & socket.AI_PASSIVE:
            return addresses
        try:
            public = bool(addresses) and all(
                public_address(str(item[4][0])) for item in addresses
            )
        except ValueError:
            public = False
        if not public:
            raise socket.gaierror(socket.EAI_NONAME, PRIVATE_ADDRESS_DENIED)
        return addresses


def gateway_master(config: GatewayConfig, opts: options.Options) -> DumpMaster:
    """The policy proxy, honoring ``opts`` except for the options set here.

    Callers choose where it listens, its CA directory and the CAs it trusts
    upstream; TLS verification and the connection options below always apply.
    It must run on a ``GatewayEventLoop``, or it refuses every upstream host.
    """
    master = DumpMaster(opts, with_termlog=False, with_dumper=False)
    cast(Any, master.options).update(
        ssl_insecure=False,
        connection_strategy="lazy",
        upstream_cert=False,
        block_global=False,
        block_private=False,
        rawtcp=False,
        anticomp=True,
    )
    cast(Any, master.addons).add(PolicyGateway(config))
    return master


async def _serve(config: GatewayConfig) -> None:
    # Loopback only: the agent reaches this solely through the MCP route above.
    bridge = await asyncio.start_server(
        lambda reader, writer: bridge_mcp_relay(reader, writer, MCP_RELAY_SOCKET),
        "127.0.0.1",
        MCP_RELAY_PORT,
    )
    opts = options.Options(listen_host="0.0.0.0", listen_port=GATEWAY_PORT, confdir="/state/ca")
    master = gateway_master(config, opts)
    async with bridge:
        await master.run()


def serve(config_path: Path) -> None:
    config = GatewayConfig.model_validate_json(config_path.read_text())
    with asyncio.Runner(loop_factory=GatewayEventLoop) as runner:
        runner.run(_serve(config))


if __name__ == "__main__":
    serve(Path(sys.argv[1]))
