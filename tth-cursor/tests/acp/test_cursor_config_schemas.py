"""Cursor ACP configuration schema parsing tests."""

from __future__ import annotations

import pytest
from tth_types.enums import ErrorCode
from tth_types.errors import DomainError

from tth_cursor.acp.schemas.cursor_ext import (
    CursorSelectConfigOption,
    parse_cursor_config_options,
)


def test_parse_config_options_happy_path() -> None:
    options = parse_cursor_config_options(
        {
            "sessionId": "s1",
            "configOptions": [
                {
                    "id": "model",
                    "category": "model",
                    "type": "select",
                    "currentValue": "default",
                    "options": [{"name": "Auto", "value": "default"}],
                }
            ],
        }
    )
    assert len(options) == 1
    assert isinstance(options[0], CursorSelectConfigOption)
    assert options[0].id == "model"
    assert options[0].currentValue == "default"


def test_parse_config_options_requires_object() -> None:
    with pytest.raises(DomainError) as exc:
        parse_cursor_config_options(["not", "an", "object"])
    assert exc.value.code is ErrorCode.PROTOCOL_ERROR


def test_parse_config_options_requires_list() -> None:
    with pytest.raises(DomainError) as exc:
        parse_cursor_config_options({"configOptions": {}})
    assert exc.value.code is ErrorCode.PROTOCOL_ERROR


def test_parse_config_options_rejects_malformed_entry() -> None:
    with pytest.raises(DomainError) as exc:
        parse_cursor_config_options(
            {
                "configOptions": [
                    {
                        "id": "model",
                        "category": "model",
                        "type": "select",
                        "currentValue": "default",
                        "options": "not-a-list",
                    }
                ]
            }
        )
    assert exc.value.code is ErrorCode.PROTOCOL_ERROR


def test_update_todos_validator_accepts_observed_shape() -> None:
    from tth_cursor.acp.schemas.cursor_ext import is_allowlisted_cursor_update_todos

    assert is_allowlisted_cursor_update_todos(
        {
            "toolCallId": "tc-1",
            "merge": False,
            "todos": [
                {"id": "a", "content": "Write docstrings", "status": "pending"},
                {"id": "b", "content": "Run tests", "status": "in_progress"},
                {"id": "c", "content": "Commit", "status": "completed"},
                {"id": "d", "content": "Skip this", "status": "cancelled"},
            ],
        }
    )
    # merge is optional
    assert is_allowlisted_cursor_update_todos(
        {"toolCallId": "tc-1", "todos": [{"id": "a", "content": "x", "status": "pending"}]}
    )


def test_update_todos_validator_tolerates_additive_fields() -> None:
    """Ack-only extension: new Cursor fields or statuses must not cancel the turn."""
    from tth_cursor.acp.schemas.cursor_ext import is_allowlisted_cursor_update_todos

    assert is_allowlisted_cursor_update_todos(
        {"toolCallId": "tc-1", "todos": [{"id": "a", "content": "x", "status": "paused"}]}
    )
    assert is_allowlisted_cursor_update_todos(
        {
            "toolCallId": "tc-1",
            "todos": [{"id": "a", "content": "x", "status": "pending", "priority": 2}],
            "network": True,
        }
    )


def test_update_todos_validator_rejects_unknown_shapes() -> None:
    from tth_cursor.acp.schemas.cursor_ext import is_allowlisted_cursor_update_todos

    assert not is_allowlisted_cursor_update_todos(None)
    assert not is_allowlisted_cursor_update_todos({})
    # Structural breaks still fail closed: wrong types, missing fields.
    assert not is_allowlisted_cursor_update_todos({"toolCallId": "tc-1", "todos": "all"})
    assert not is_allowlisted_cursor_update_todos(
        {"toolCallId": "tc-1", "todos": [{"id": "a", "status": "pending"}]}
    )
