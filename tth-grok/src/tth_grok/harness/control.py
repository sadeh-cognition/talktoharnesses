"""Shared Grok ACP initialization, validation, and headless authentication."""

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
    auth_methods = result_map.get("authMethods")
    if auth_methods:
        methods = {_map_dict(method).get("id") for method in cast(list[object], auth_methods)}
        if os.environ.get("XAI_API_KEY") and "xai.api_key" in methods:
            method_id = "xai.api_key"
        elif "cached_token" in methods:
            method_id = "cached_token"
        else:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "Grok credentials are unavailable; seed the sandbox from a Grok login "
                "or pass XAI_API_KEY into the sandbox",
                details={"reason": "authentication_required"},
            )
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
    return result_map


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
