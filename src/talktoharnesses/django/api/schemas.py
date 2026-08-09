"""Request-only models for the Django-Ninja surface (not shared wire projections)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from talktoharnesses.domain.enums import ApprovalDecision, ApprovalRuleDecision, HarnessKind
from talktoharnesses.domain.models import (
    ApprovalMatcher,
    ApprovalRuleScope,
    HarnessConfiguration,
)

# Request models are non-strict so JSON strings coerce to enums.
_REQUEST = ConfigDict(extra="forbid", strict=False)


class HarnessConfigurationBody(BaseModel):
    """Wire request shape for harness configuration (coerces JSON enums)."""

    model_config = _REQUEST

    kind: HarnessKind
    executable_path: str | None = None
    model: str | None = None
    mode: str | None = None
    working_directory: str
    workspace_roots: tuple[str, ...] = ()

    def to_domain(self) -> HarnessConfiguration:
        return HarnessConfiguration(
            kind=self.kind,
            executable_path=self.executable_path,
            model=self.model,
            mode=self.mode,
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
    model: str | None = None


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


class ApprovalRuleBody(BaseModel):
    """Create/replace rule payload using the public discriminated unions."""

    model_config = _REQUEST

    decision: ApprovalRuleDecision
    scope: ApprovalRuleScope
    matcher: ApprovalMatcher

    @field_validator("scope", mode="before")
    @classmethod
    def _scope(cls, value: object) -> ApprovalRuleScope:
        encoded = value.model_dump_json() if isinstance(value, BaseModel) else json.dumps(value)
        return cast(
            ApprovalRuleScope,
            TypeAdapter(ApprovalRuleScope).validate_json(encoded),
        )

    @field_validator("matcher", mode="before")
    @classmethod
    def _matcher(cls, value: object) -> ApprovalMatcher:
        encoded = value.model_dump_json() if isinstance(value, BaseModel) else json.dumps(value)
        return cast(
            ApprovalMatcher,
            TypeAdapter(ApprovalMatcher).validate_json(encoded),
        )


class ResolveInteractionBody(BaseModel):
    model_config = _REQUEST

    decision: ApprovalDecision | None = None
    answers: dict[str, Any] | None = None
    create_rule: ApprovalRuleBody | None = None
