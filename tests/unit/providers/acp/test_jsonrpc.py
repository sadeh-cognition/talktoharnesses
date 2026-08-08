"""JSON-RPC envelope strictness."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from talktoharnesses.providers.acp.jsonrpc import (
    JsonRpcErrorResponse,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcSuccessResponse,
    parse_envelope,
)


def test_request_and_notification() -> None:
    req = parse_envelope({"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}})
    assert isinstance(req, JsonRpcRequest)
    note = parse_envelope({"jsonrpc": "2.0", "method": "session/cancel", "params": {}})
    assert isinstance(note, JsonRpcNotification)


def test_success_and_error() -> None:
    ok = parse_envelope({"jsonrpc": "2.0", "id": "a", "result": {"x": 1}})
    assert isinstance(ok, JsonRpcSuccessResponse)
    err = parse_envelope({"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": "nope"}})
    assert isinstance(err, JsonRpcErrorResponse)


def test_bool_id_rejected() -> None:
    with pytest.raises(ValidationError):
        JsonRpcRequest.model_validate({"jsonrpc": "2.0", "method": "x", "id": True, "params": {}})


def test_batch_array_rejected() -> None:
    with pytest.raises(ValueError, match="batch"):
        parse_envelope([{"jsonrpc": "2.0", "method": "x"}])


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        JsonRpcNotification.model_validate(
            {"jsonrpc": "2.0", "method": "x", "params": {}, "extra": 1}
        )
