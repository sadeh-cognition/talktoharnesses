"""Shared Cursor ACP initialization and configuration operations."""

from __future__ import annotations

from typing import Any, cast

from talktoharnesses import __version__
from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.providers.acp.connection import AcpConnection
from talktoharnesses.providers.acp.jsonrpc import JsonRpcRemoteError
from talktoharnesses.providers.acp.protocol import CURSOR_ALLOWED_OUTBOUND_METHODS
from talktoharnesses.providers.acp.schemas.cursor_ext import (
    CursorSelectConfigOption,
    parse_cursor_config_options,
)
from talktoharnesses.providers.cursor.compatibility import CursorReleaseRecord

CLIENT_INFO = {"name": "talktoharnesses", "version": __version__}


def _map_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    raw = cast(dict[object, object], cast(object, value))
    return {str(key): item for key, item in raw.items()}


async def initialize_cursor(
    connection: AcpConnection,
    release: CursorReleaseRecord,
) -> dict[str, Any]:
    future, _ = await connection.request(
        "initialize",
        {
            "protocolVersion": 1,
            "clientInfo": CLIENT_INFO,
            "clientCapabilities": {
                "_meta": {
                    "parameterizedModelPicker": True,
                }
            },
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
    agent_info = _map_dict(result_map.get("agentInfo"))
    if agent_info and (
        agent_info.get("name") != release.agent_name
        or agent_info.get("version") != release.cli_version
    ):
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "initialize agent identity does not match compatibility record",
            details={"agentInfo": agent_info, "release_id": release.id},
        )
    missing_methods = set(release.required_agent_methods) - CURSOR_ALLOWED_OUTBOUND_METHODS
    if missing_methods:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "adapter does not implement required agent methods",
            details={"missing_methods": sorted(missing_methods)},
        )
    return result_map


def find_cursor_config_option(
    options: tuple[CursorSelectConfigOption, ...],
    config_id: str,
) -> CursorSelectConfigOption | None:
    return next((option for option in options if option.id == config_id), None)


async def set_cursor_config_option(
    connection: AcpConnection,
    *,
    session_id: str,
    config_id: str,
    value: str,
    options: tuple[CursorSelectConfigOption, ...],
) -> tuple[CursorSelectConfigOption, ...]:
    option = find_cursor_config_option(options, config_id)
    if option is None:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "Cursor configuration option is not advertised",
            details={"config_id": config_id},
        )
    advertised = tuple(item.value for item in option.options)
    if value not in advertised:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "Cursor configuration value is not advertised",
            details={
                "config_id": config_id,
                "value": value,
                "advertised_values": list(advertised),
            },
        )
    if option.currentValue == value:
        return options
    try:
        future, _ = await connection.request(
            "session/set_config_option",
            {
                "sessionId": session_id,
                "configId": config_id,
                "value": value,
            },
        )
        result = await future
    except JsonRpcRemoteError as exc:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "Cursor rejected configuration option",
            details={"config_id": config_id, "remote_code": exc.code},
        ) from exc
    new_options = parse_cursor_config_options(result)
    updated = find_cursor_config_option(new_options, config_id)
    if updated is None or updated.currentValue != value:
        raise DomainError(
            ErrorCode.PROTOCOL_ERROR,
            "Cursor set_config_option response did not reflect requested value",
            details={
                "config_id": config_id,
                "requested": value,
                "currentValue": None if updated is None else updated.currentValue,
            },
        )
    return new_options
