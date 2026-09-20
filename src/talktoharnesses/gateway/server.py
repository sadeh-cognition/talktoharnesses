"""HTTPS gateway process. Never run this process inside an agent container."""

from __future__ import annotations

import asyncio
import fcntl
import hmac
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
from pydantic import BaseModel, Field
from tth_types.enums import HarnessKind
from tth_types.sandbox import CommandCheck, SandboxPolicyRevision

from talktoharnesses.command_policy import check_command
from talktoharnesses.gateway.credentials import CredentialVault
from talktoharnesses.gateway.routes import (
    GATEWAY_HOST,
    GATEWAY_PORT,
    KIND_PROVIDERS,
    META_API_HOST,
    PROVIDER_ROUTES,
    ProviderRoute,
    TokenExchange,
    normalized_path,
    permitted_request,
    provider_route,
    public_address,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.WARNING)
logger.propagate = False


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


class PolicyGateway:
    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self.vault = CredentialVault(
            kind=config.kind,
            seed=config.seed,
            auth_file=Path(config.auth_file) if config.auth_file else None,
            api_keys=config.api_keys,
            cursor_login_file=Path(config.cursor_login_file)
            if config.kind is HarnessKind.CURSOR
            else None,
        )

    def _deny(self, flow: http.HTTPFlow, reason: str = "egress_denied") -> None:
        # Do not record paths, query strings, bodies or headers. They can contain
        # credentials or arbitrary text even when a request was denied.
        logger.warning(
            "sandbox_policy_denied policy=%s revision=%s reason=%s",
            self.config.revision.ref.id,
            self.config.revision.ref.revision,
            reason,
        )
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
        host = flow.request.host.lower().rstrip(".")
        hosts = {rule.host for rule in self.config.revision.policy.egress}
        hosts.update(
            route.rule.host
            for route in PROVIDER_ROUTES
            if route.provider in KIND_PROVIDERS[self.config.kind]
        )
        if flow.request.port != 443 or host not in hosts:
            self._deny(flow)

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
        if request.scheme != "https" or request.port != 443:
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

    async def server_connect(self, data: server_hooks.ServerConnectionHookData) -> None:
        address = data.server.address
        if address == (self.config.split_address, 8010):
            return
        hosts = {rule.host for rule in self.config.revision.policy.egress}
        hosts.update(
            route.rule.host
            for route in PROVIDER_ROUTES
            if route.provider in KIND_PROVIDERS[self.config.kind]
        )
        if address is None or address[1] != 443 or address[0].lower().rstrip(".") not in hosts:
            data.server.error = "Sandbox policy denied this connection."
            return
        try:
            # Pin the address at the connection hook. mitmproxy selects a fresh
            # connection after requestheaders, so pinning there is too early.
            addresses = await asyncio.get_running_loop().getaddrinfo(
                address[0],
                address[1],
                type=socket.SOCK_STREAM,
            )
            if not addresses or any(not public_address(str(item[4][0])) for item in addresses):
                data.server.error = "Sandbox policy denied this address."
                return
            data.server.address = (str(addresses[0][4][0]), 443)
            data.server.sni = address[0]
        except (OSError, ValueError):
            data.server.error = "Sandbox gateway could not resolve this address."

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


async def serve(config_path: Path) -> None:
    config = GatewayConfig.model_validate_json(config_path.read_text())
    opts = options.Options(
        listen_host="0.0.0.0", listen_port=GATEWAY_PORT, confdir="/state/ca", ssl_insecure=False
    )
    master = DumpMaster(opts, with_termlog=False, with_dumper=False)
    cast(Any, master.options).update(
        connection_strategy="lazy",
        upstream_cert=False,
        block_global=False,
        block_private=False,
        rawtcp=False,
        anticomp=True,
    )
    cast(Any, master.addons).add(PolicyGateway(config))
    await master.run()


if __name__ == "__main__":
    asyncio.run(serve(Path(sys.argv[1])))
