"""MCP server configuration: validation, wire round trip, and provider mappings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError
from tth_types.harness import (
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessMcpHeader,
    HarnessMcpServer,
)
from tth_types.mcp import (
    acp_mcp_servers,
    claude_mcp_servers,
    codex_mcp_overrides,
    require_mcp_servers_supported,
)


def _server(name: str = "memory", **overrides: object) -> HarnessMcpServer:
    fields: dict[str, object] = {
        "name": name,
        "url": "http://127.0.0.1:8001/mcp/projects/7/memory",
        "headers": (HarnessMcpHeader(name="Authorization", value="Bearer tok"),),
    }
    fields.update(overrides)
    return HarnessMcpServer.model_validate(fields)


def _config(*servers: HarnessMcpServer) -> HarnessConfiguration:
    return HarnessConfiguration(
        kind=HarnessKind.CLAUDE, working_directory="/tmp", mcp_servers=servers
    )


def test_configuration_defaults_to_no_mcp_servers() -> None:
    config = HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp")
    assert config.mcp_servers == ()
    assert config.model_dump(mode="json")["mcp_servers"] == []
    assert HarnessCapabilities(kind=HarnessKind.GROK, version="1").supports_mcp_servers is False


def test_configuration_round_trips_mcp_servers_through_json() -> None:
    config = _config(_server())
    decoded = HarnessConfiguration.model_validate_json(config.model_dump_json())
    assert decoded == config


@pytest.mark.parametrize("url", ["", "memory", "ftp://host/x", "http:///no-host", "/relative"])
def test_server_requires_absolute_http_url(url: str) -> None:
    with pytest.raises(ValidationError, match="absolute http"):
        _server(url=url)


@pytest.mark.parametrize("name", ["", "has space", "slash/name", "x" * 65])
def test_server_name_is_a_short_identifier(name: str) -> None:
    with pytest.raises(ValidationError):
        _server(name=name)


def test_header_name_must_be_a_token() -> None:
    with pytest.raises(ValidationError):
        HarnessMcpHeader(name="Bad Header", value="x")


def test_configuration_rejects_duplicate_server_names() -> None:
    with pytest.raises(ValidationError, match="unique"):
        _config(_server("a"), _server("a"))


def _capabilities(*, supports_mcp_servers: bool) -> HarnessCapabilities:
    return HarnessCapabilities(
        kind=HarnessKind.OPENCODE, version="1", supports_mcp_servers=supports_mcp_servers
    )


def test_require_mcp_servers_supported_passes_without_servers() -> None:
    require_mcp_servers_supported(_config(), _capabilities(supports_mcp_servers=False))


def test_require_mcp_servers_supported_passes_when_probe_advertises_support() -> None:
    require_mcp_servers_supported(_config(_server()), _capabilities(supports_mcp_servers=True))


def test_require_mcp_servers_supported_rejects_unsupported_kind() -> None:
    with pytest.raises(DomainError) as exc:
        require_mcp_servers_supported(_config(_server()), _capabilities(supports_mcp_servers=False))
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
    assert "opencode does not support configured MCP servers" in exc.value.message
    assert exc.value.details["mcp_servers"] == ["memory"]


def test_claude_mapping_uses_http_server_configs() -> None:
    assert claude_mcp_servers(_config()) == {}
    assert claude_mcp_servers(_config(_server(), _server("bare", headers=()))) == {
        "memory": {
            "type": "http",
            "url": "http://127.0.0.1:8001/mcp/projects/7/memory",
            "headers": {"Authorization": "Bearer tok"},
        },
        "bare": {"type": "http", "url": "http://127.0.0.1:8001/mcp/projects/7/memory"},
    }


def test_acp_mapping_lists_http_servers_with_header_pairs() -> None:
    assert acp_mcp_servers(_config()) == []
    assert acp_mcp_servers(_config(_server())) == [
        {
            "type": "http",
            "name": "memory",
            "url": "http://127.0.0.1:8001/mcp/projects/7/memory",
            "headers": [{"name": "Authorization", "value": "Bearer tok"}],
        }
    ]


def test_codex_mapping_emits_toml_overrides_with_escaping() -> None:
    assert codex_mcp_overrides(_config()) == ()
    tricky = _server(
        "memory",
        headers=(HarnessMcpHeader(name="X-Note", value='say "hi"\\now'),),
    )
    assert codex_mcp_overrides(_config(tricky)) == (
        'mcp_servers.memory.url="http://127.0.0.1:8001/mcp/projects/7/memory"',
        'mcp_servers.memory.http_headers={ "X-Note" = "say \\"hi\\"\\\\now" }',
    )
    assert codex_mcp_overrides(_config(_server("bare", headers=()))) == (
        'mcp_servers.bare.url="http://127.0.0.1:8001/mcp/projects/7/memory"',
    )
