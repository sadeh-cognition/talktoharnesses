"""Cursor ACP configuration schema parsing tests."""

from __future__ import annotations

import pytest

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.providers.acp.schemas.cursor_ext import (
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
