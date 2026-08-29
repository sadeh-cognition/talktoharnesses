"""Strict JSON-RPC 2.0 envelopes for ACP framing."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Forbid(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _reject_bool_id(value: Any) -> str | int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        msg = "JSON-RPC id must be string or integer (not boolean)"
        raise ValueError(msg)
    return value


class JsonRpcRequest(_Forbid):
    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: dict[str, Any] | list[Any] | None = None
    id: str | int

    @field_validator("id", mode="before")
    @classmethod
    def _id(cls, value: Any) -> str | int:
        return _reject_bool_id(value)


class JsonRpcNotification(_Forbid):
    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: dict[str, Any] | list[Any] | None = None

    @model_validator(mode="after")
    def _no_id(self) -> JsonRpcNotification:
        # Notifications must not carry an id field (enforced by schema absence).
        return self


class JsonRpcError(_Forbid):
    code: int
    message: str
    data: Any | None = None


class JsonRpcSuccessResponse(_Forbid):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int
    result: Any = None

    @field_validator("id", mode="before")
    @classmethod
    def _id(cls, value: Any) -> str | int:
        return _reject_bool_id(value)


class JsonRpcErrorResponse(_Forbid):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None
    error: JsonRpcError

    @field_validator("id", mode="before")
    @classmethod
    def _id(cls, value: Any) -> str | int | None:
        if value is None:
            return None
        return _reject_bool_id(value)


class JsonRpcOutboundRequest(_Forbid):
    """Outbound request body before serialization."""

    method: str
    params: dict[str, Any] | None = None
    id: str | int


def parse_envelope(
    obj: object,
) -> JsonRpcRequest | JsonRpcNotification | JsonRpcSuccessResponse | JsonRpcErrorResponse:
    """Parse one JSON object as a strict JSON-RPC envelope."""
    if isinstance(obj, list):
        msg = "JSON-RPC batch arrays are not supported"
        raise ValueError(msg)
    if not isinstance(obj, dict):
        msg = "JSON-RPC envelope must be an object"
        raise ValueError(msg)

    if "method" in obj:
        if "id" in obj:
            return JsonRpcRequest.model_validate(obj)
        return JsonRpcNotification.model_validate(obj)
    if "error" in obj:
        return JsonRpcErrorResponse.model_validate(obj)
    if "result" in obj:
        return JsonRpcSuccessResponse.model_validate(obj)
    msg = "invalid JSON-RPC envelope"
    raise ValueError(msg)


def encode_frame(payload: dict[str, Any]) -> bytes:
    """Serialize one JSON-RPC object as a newline-delimited UTF-8 frame."""
    import json

    return (json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


class ProtocolCloseError(Exception):
    """Raised when pending requests are failed due to connection close."""

    def __init__(self, message: str = "ACP connection closed") -> None:
        super().__init__(message)
        self.message = message


class JsonRpcRemoteError(Exception):
    """JSON-RPC error response from the peer."""

    def __init__(self, code: int, message: str, data: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# Silence unused Field import for future params models.
_ = Field
