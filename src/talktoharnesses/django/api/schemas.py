"""Request-only models for the Django-Ninja surface (not shared wire projections)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from talktoharnesses.domain.enums import ApprovalDecision, HarnessKind
from talktoharnesses.domain.models import (
    ApprovalRuleInput,
    HarnessConfiguration,
)

# Request models are non-strict so JSON strings coerce to enums.
_REQUEST = ConfigDict(extra="forbid", strict=False)

_CURSOR_MODEL_SELECTOR_DESCRIPTION = (
    "For Cursor, this existing string field accepts either a model ID or "
    "`model[key=value,...]`, for example `composer-2.5[fast=false]`. "
    "Parameter names and values are model-specific and must be advertised by Cursor."
)


class HarnessConfigurationBody(BaseModel):
    """Wire request shape for harness configuration (coerces JSON enums)."""

    model_config = _REQUEST

    kind: HarnessKind
    executable_path: str | None = None
    model: str | None = Field(
        default=None,
        description=(
            "Provider model selector used as the session baseline. "
            f"{_CURSOR_MODEL_SELECTOR_DESCRIPTION}"
        ),
    )
    mode: str | None = None
    force: bool = False
    working_directory: str
    workspace_roots: tuple[str, ...] = ()

    def to_domain(self) -> HarnessConfiguration:
        return HarnessConfiguration(
            kind=self.kind,
            executable_path=self.executable_path,
            model=self.model,
            mode=self.mode,
            force=self.force,
            working_directory=self.working_directory,
            workspace_roots=self.workspace_roots,
        )


class CreateHarnessBody(BaseModel):
    model_config = _REQUEST

    name: str = Field(min_length=1)
    configuration: HarnessConfigurationBody


class CreateConversationBody(BaseModel):
    model_config = _REQUEST

    harness_id: UUID
    title: str | None = None


class ImportTranscriptBody(BaseModel):
    model_config = _REQUEST

    harness_id: UUID
    document: dict[str, Any]


class SnoozeBody(BaseModel):
    model_config = _REQUEST

    until: datetime


class RetentionPolicyBody(BaseModel):
    model_config = _REQUEST

    months: int = Field(ge=1, le=120)


class RetentionExemptionBody(BaseModel):
    model_config = _REQUEST

    exempt: bool


class SubmitTurnBody(BaseModel):
    model_config = _REQUEST

    prompt: str = Field(min_length=1)
    model: str | None = Field(
        default=None,
        description=(
            "Optional one-turn provider model override. "
            f"{_CURSOR_MODEL_SELECTOR_DESCRIPTION} "
            "For Cursor, the session baseline is restored before the next turn without an override."
        ),
    )


class EditQueuedPromptBody(BaseModel):
    model_config = _REQUEST

    prompt: str = Field(min_length=1)


class SteerBody(BaseModel):
    model_config = _REQUEST

    prompt: str = Field(min_length=1)


class SwitchHarnessBody(BaseModel):
    model_config = _REQUEST

    harness_id: UUID


class InteractionDraftBody(BaseModel):
    model_config = _REQUEST

    draft: dict[str, Any] = Field(default_factory=dict)


class ApprovalRuleBody(ApprovalRuleInput):
    """Create/replace rule payload using the public discriminated unions."""

    model_config = _REQUEST


class ResolveInteractionBody(BaseModel):
    model_config = _REQUEST

    decision: ApprovalDecision | None = None
    answers: dict[str, Any] | None = None
    create_rule: ApprovalRuleBody | None = None
