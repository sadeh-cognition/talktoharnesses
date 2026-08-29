"""Grok extension notification schemas (strict decode, then ignore for transcript)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GrokControlNotification(_Strict):
    """Loose retention model for allowlisted control-plane notifications.

    Fields beyond method are retained as a raw params dict after envelope
    validation; unknown *methods* are rejected at the connection layer.
    """

    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class GrokQuestionOption(_Strict):
    label: str
    description: str | None = None


class GrokQuestion(_Strict):
    id: str | None = None
    question: str
    options: list[GrokQuestionOption]
    multiSelect: bool | None = None


class GrokAskUserQuestionParams(_Strict):
    sessionId: str
    toolCallId: str
    questions: list[GrokQuestion]
    mode: str | None = None


def is_allowlisted_ask_user_question(params: dict[str, Any] | None) -> bool:
    if params is None:
        return False
    try:
        GrokAskUserQuestionParams.model_validate(params)
    except ValueError:
        return False
    return True
