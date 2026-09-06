"""MCP servers named in a harness configuration: support gate and provider shapes.

Every split maps the same ``HarnessMcpServer`` records onto its provider's
wire format. Keeping the mappings here means the four supporting kinds
(Claude Code, Cursor, Grok, Codex) cannot drift apart in how they encode a
server, and a new server field only has to be threaded through once.
"""

from __future__ import annotations

from typing import Any

from tth_types.enums import ErrorCode
from tth_types.errors import DomainError
from tth_types.harness import HarnessCapabilities, HarnessConfiguration, HarnessMcpServer


def require_mcp_servers_supported(
    config: HarnessConfiguration, capabilities: HarnessCapabilities
) -> None:
    """Reject configured MCP servers for a kind whose probe cannot attach them.

    Driven by the probed ``supports_mcp_servers`` flag, so a split whose
    compatibility document does not opt in is rejected without its adapter
    knowing MCP exists.
    """
    if not config.mcp_servers or capabilities.supports_mcp_servers:
        return
    raise DomainError(
        ErrorCode.PROVIDER_INCOMPATIBLE,
        f"{capabilities.kind.value} does not support configured MCP servers; "
        "remove mcp_servers from the harness configuration",
        details={
            "kind": config.kind.value,
            "mcp_servers": [server.name for server in config.mcp_servers],
        },
    )


def claude_mcp_servers(config: HarnessConfiguration) -> dict[str, dict[str, Any]]:
    """Claude Agent SDK ``mcp_servers`` option: name -> HTTP server config."""
    return {server.name: _claude_server(server) for server in config.mcp_servers}


def _claude_server(server: HarnessMcpServer) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": "http", "url": server.url}
    if server.headers:
        entry["headers"] = {header.name: header.value for header in server.headers}
    return entry


def acp_mcp_servers(config: HarnessConfiguration) -> list[dict[str, Any]]:
    """ACP ``session/new`` and ``session/load`` ``mcpServers`` entries."""
    return [
        {
            "type": "http",
            "name": server.name,
            "url": server.url,
            "headers": [{"name": header.name, "value": header.value} for header in server.headers],
        }
        for server in config.mcp_servers
    ]


def codex_mcp_overrides(config: HarnessConfiguration) -> tuple[str, ...]:
    """Codex ``-c`` config overrides declaring each server under ``mcp_servers``."""
    overrides: list[str] = []
    for server in config.mcp_servers:
        prefix = f"mcp_servers.{server.name}"
        overrides.append(f"{prefix}.url={_toml_string(server.url)}")
        if server.headers:
            pairs = ", ".join(
                f"{_toml_string(header.name)} = {_toml_string(header.value)}"
                for header in server.headers
            )
            overrides.append(f"{prefix}.http_headers={{ {pairs} }}")
    return tuple(overrides)


def _toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    )
    return f'"{escaped}"'
