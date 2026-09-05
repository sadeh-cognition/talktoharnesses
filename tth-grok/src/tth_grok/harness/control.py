"""Shared Grok ACP initialization, validation, and headless authentication.

ACP advertises ``authMethods`` on ``initialize`` as the methods a client *may*
use; whether authentication is actually needed is signalled by the agent
rejecting ``session/new`` / ``session/load`` with the ``auth_required`` error.
Authentication therefore happens lazily, on that error, never eagerly.
"""

from __future__ import annotations

import os
from typing import Any, cast

from tth_types.enums import ErrorCode
from tth_types.errors import DomainError

from tth_grok import __version__
from tth_grok.acp.connection import AcpConnection
from tth_grok.acp.jsonrpc import JsonRpcRemoteError
from tth_grok.acp.schemas.base import ALLOWED_OUTBOUND_METHODS
from tth_grok.harness.compatibility import GrokReleaseRecord

CLIENT_INFO = {"name": "talktoharnesses", "version": __version__}

# ACP: the agent requires authentication before the requested operation.
ACP_AUTH_REQUIRED = -32000


def _map_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    raw = cast(dict[object, object], cast(object, value))
    return {str(key): item for key, item in raw.items()}


async def initialize_grok(
    connection: AcpConnection,
    release: GrokReleaseRecord,
    *,
    require_load_session: bool = False,
) -> dict[str, Any]:
    future, _ = await connection.request(
        "initialize",
        {
            "protocolVersion": 1,
            "clientInfo": CLIENT_INFO,
            # No client fs/terminal capabilities unless fixtures prove reverse handlers.
            "clientCapabilities": {},
        },
    )
    result = await future
    if not isinstance(result, dict):
        raise DomainError(ErrorCode.PROTOCOL_ERROR, "initialize result must be an object")
    result_map = _map_dict(cast(object, result))
    protocol = result_map.get("protocolVersion")
    if protocol != 1:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "ACP protocol version mismatch",
            details={"protocolVersion": protocol},
        )
    if protocol != release.acp_protocol_version:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "ACP protocol version does not match compatibility record",
            details={
                "protocolVersion": protocol,
                "expected": release.acp_protocol_version,
            },
        )
    validate_grok_initialize(
        result_map,
        release,
        require_load_session=require_load_session,
    )
    return result_map


def advertised_auth_methods(initialize_result: dict[str, Any]) -> frozenset[str]:
    """Method ids the agent offered on ``initialize`` (possibly none)."""
    raw = initialize_result.get("authMethods")
    if not isinstance(raw, list):
        return frozenset()
    ids: set[str] = set()
    for method in cast(list[object], raw):
        method_id = _map_dict(method).get("id")
        if isinstance(method_id, str):
            ids.add(method_id)
    return frozenset(ids)


def _select_auth_method(auth_methods: frozenset[str]) -> str:
    if os.environ.get("XAI_API_KEY") and "xai.api_key" in auth_methods:
        return "xai.api_key"
    if "cached_token" in auth_methods:
        return "cached_token"
    raise DomainError(
        ErrorCode.PROVIDER_INCOMPATIBLE,
        "Grok credentials are unavailable; seed the sandbox from a Grok login "
        "or pass XAI_API_KEY into the sandbox",
        details={"reason": "authentication_required", "auth_methods": sorted(auth_methods)},
    )


async def authenticate_grok(connection: AcpConnection, auth_methods: frozenset[str]) -> str:
    """Run one headless ``authenticate`` with the best advertised method."""
    method_id = _select_auth_method(auth_methods)
    try:
        future, _ = await connection.request(
            "authenticate", {"methodId": method_id, "_meta": {"headless": True}}
        )
        await future
    except JsonRpcRemoteError as exc:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "Grok authentication failed; refresh the sandbox Grok login or XAI_API_KEY",
            details={
                "reason": "authentication_failed",
                "method_id": method_id,
                "remote_code": exc.code,
            },
        ) from exc
    return method_id


async def request_with_authentication(
    connection: AcpConnection,
    method: str,
    params: dict[str, Any],
    *,
    auth_methods: frozenset[str],
) -> Any:
    """Send ``method``; on ACP ``auth_required`` authenticate once and retry."""
    future, _ = await connection.request(method, params)
    try:
        return await future
    except JsonRpcRemoteError as exc:
        if exc.code != ACP_AUTH_REQUIRED:
            raise
    method_id = await authenticate_grok(connection, auth_methods)
    future, _ = await connection.request(method, params)
    try:
        return await future
    except JsonRpcRemoteError as exc:
        if exc.code != ACP_AUTH_REQUIRED:
            raise
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "Grok still requires authentication after a headless login",
            details={
                "reason": "authentication_failed",
                "method_id": method_id,
                "remote_code": exc.code,
            },
        ) from exc


def validate_grok_initialize(
    result: dict[str, Any],
    release: GrokReleaseRecord,
    *,
    require_load_session: bool = False,
) -> None:
    agent_info = _map_dict(result.get("agentInfo"))
    meta = _map_dict(result.get("_meta"))
    # Grok 1.0.0 may omit agentInfo; identity then lives in _meta.agentVersion.
    version = agent_info.get("version") or meta.get("agentVersion")
    name = agent_info.get("name")
    name_ok = name == release.agent_name if name is not None else version == release.cli_version
    if not name_ok or version != release.cli_version:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "initialize agent identity does not match compatibility record",
            details={
                "agentInfo": agent_info,
                "agentVersion": meta.get("agentVersion"),
                "release_id": release.id,
            },
        )
    capabilities = _map_dict(result.get("agentCapabilities"))
    if require_load_session and capabilities.get("loadSession") is not True:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "initialize result does not advertise session loading",
            details={"release_id": release.id},
        )
    missing_methods = set(release.required_agent_methods) - ALLOWED_OUTBOUND_METHODS
    if missing_methods:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "adapter does not implement required agent methods",
            details={"missing_methods": sorted(missing_methods)},
        )
