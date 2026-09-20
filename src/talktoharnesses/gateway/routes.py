"""Request routing. A permitted hostname alone never grants credential access."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from tth_types.enums import HarnessKind
from tth_types.sandbox import EgressRule, SandboxPolicy

GATEWAY_HOST = "tth-gateway.invalid"
GATEWAY_PORT = 8080
META_API_HOST = "api.meta.ai"
META_GATEWAY_BASE = f"http://{GATEWAY_HOST}:{GATEWAY_PORT}/v1"


@dataclass(frozen=True)
class ProviderRoute:
    provider: str
    rule: EgressRule
    refresh: bool = False


def _routes(provider: str, host: str, paths: tuple[str, ...]) -> tuple[ProviderRoute, ...]:
    return tuple(
        ProviderRoute(provider, EgressRule(host=host, path=path, methods=("GET", "HEAD", "POST")))
        for path in paths
    )


PROVIDER_ROUTES = (
    *_routes("openai", "api.openai.com", ("/v1/responses", "/v1/chat/completions", "/v1/models")),
    *_routes(
        "openai",
        "chatgpt.com",
        (
            "/backend-api/codex/responses",
            "/backend-api/codex/models",
            "/backend-api/wham/usage",
        ),
    ),
    ProviderRoute(
        "openai", EgressRule(host="auth.openai.com", path="/oauth/token", methods=("POST",)), True
    ),
    *_routes(
        "anthropic",
        "api.anthropic.com",
        ("/v1/messages", "/v1/models", "/api/oauth/profile", "/api/oauth/usage"),
    ),
    *(
        ProviderRoute("anthropic", EgressRule(host=host, path=path, methods=("POST",)), True)
        for host, path in (
            ("console.anthropic.com", "/v1/oauth/token"),
            ("platform.claude.com", "/v1/oauth/token"),
            ("claude.ai", "/api/oauth/token"),
        )
    ),
    *_routes("xai", "api.x.ai", ("/v1/chat/completions", "/v1/responses", "/v1/models")),
    *_routes(
        "xai",
        "cli-chat-proxy.grok.com",
        (
            "/v1/chat/completions",
            "/v1/responses",
            "/v1/models",
            "/settings",
        ),
    ),
    ProviderRoute(
        "xai", EgressRule(host="auth.x.ai", path="/oauth2/token", methods=("POST",)), True
    ),
    *_routes("xai", "auth.x.ai", ("/.well-known/openid-configuration",)),
    *(
        _route
        for host in ("api2.cursor.sh", "api2direct.cursor.sh")
        for _route in _routes(
            "cursor",
            host,
            (
                "/aiserver.v1.AiService/GetUsableModels",
                "/aiserver.v1.AiService/AvailableModels",
                "/aiserver.v1.AiService/GetDefaultModelForCli",
                "/aiserver.v1.AiService/StreamChat",
                "/aiserver.v1.AiService/StreamChatComposer",
                "/aiserver.v1.ServerConfigService/GetServerConfig",
                "/aiserver.v1.DashboardService/GetUserPrivacyMode",
                "/aiserver.v1.DashboardService/GetMe",
                "/aiserver.v1.DashboardService/GetManagedSkills",
                "/aiserver.v1.DashboardService/GetGlobalCommands",
                "/aiserver.v1.AnalyticsService/BootstrapStatsig",
                "/agent.v1.AgentService/Run",
                "/agent.v1.AgentService/RunSSE",
                "/auth/full_stripe_profile",
            ),
        )
    ),
    ProviderRoute(
        "cursor",
        EgressRule(host="api2.cursor.sh", path="/auth/exchange_user_api_key", methods=("POST",)),
        True,
    ),
    *_routes(
        "cursor",
        "agentn.global.api5.cursor.sh",
        ("/agent.v1.AgentService/Run", "/agent.v1.AgentService/RunSSE"),
    ),
    *_routes(
        "prime",
        "api.pinference.ai",
        tuple(
            prefix + path
            for prefix in ("/api/v1", "/v1")
            for path in ("/chat/completions", "/responses", "/models")
        ),
    ),
    *_routes(
        "opencode",
        "opencode.ai",
        (
            "/zen/v1/chat/completions",
            "/zen/v1/responses",
            "/zen/v1/messages",
            "/zen/v1/models",
        ),
    ),
    ProviderRoute("opencode", EgressRule(host="models.opencode.ai", path="/api.json")),
    *_routes("meta", META_API_HOST, ("/v1/responses", "/v1/chat/completions", "/v1/models")),
    ProviderRoute("meta", EgressRule(host=META_API_HOST, path="/muse-code/models")),
    *_routes("meta", "api.llama.com", ("/v1/chat/completions", "/v1/models")),
)

KIND_PROVIDERS: dict[HarnessKind, frozenset[str]] = {
    HarnessKind.CODEX: frozenset({"openai"}),
    HarnessKind.CLAUDE: frozenset({"anthropic"}),
    HarnessKind.CURSOR: frozenset({"cursor"}),
    HarnessKind.GROK: frozenset({"xai"}),
    HarnessKind.MUSE: frozenset({"meta"}),
    HarnessKind.OPENCODE: frozenset({"opencode", "openai", "anthropic", "xai", "prime", "meta"}),
    HarnessKind.PRIME_AGENT: frozenset({"openai", "anthropic", "xai", "prime"}),
}


def public_address(address: str) -> bool:
    value = ipaddress.ip_address(address)
    if isinstance(value, ipaddress.IPv6Address) and value.ipv4_mapped is not None:
        value = value.ipv4_mapped
    return value.is_global and not value.is_multicast


def normalized_path(raw: str) -> str | None:
    path = urlsplit(raw).path
    decoded = unquote(path)
    if "%" in decoded or "\\" in decoded or ".." in decoded.split("/") or "\0" in decoded:
        return None
    if not decoded.startswith("/") or decoded.startswith("//"):
        return None
    return decoded


def matches(rule: EgressRule, host: str, path: str, method: str) -> bool:
    prefix = rule.path.rstrip("/")
    return (
        host.lower().rstrip(".") == rule.host
        and method in rule.methods
        and (path == prefix or path.startswith(prefix + "/"))
    )


def provider_route(kind: HarnessKind, host: str, path: str, method: str) -> ProviderRoute | None:
    return next(
        (
            route
            for route in PROVIDER_ROUTES
            if route.provider in KIND_PROVIDERS[kind] and matches(route.rule, host, path, method)
        ),
        None,
    )


def permitted_request(policy: SandboxPolicy, host: str, raw_path: str, method: str) -> bool:
    path = normalized_path(raw_path)
    if path is None:
        return False
    # Git writes remain forbidden even when a project permits reads from a forge.
    if "git-receive-pack" in unquote(raw_path).lower():
        return False
    return any(matches(rule, host, path, method) for rule in policy.egress)
