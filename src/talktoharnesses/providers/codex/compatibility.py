"""Codex compatibility floor and probed-identity matching."""

from __future__ import annotations

import json
import sys
from functools import lru_cache
from importlib import resources

from pydantic import BaseModel, ConfigDict, Field

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import HarnessCapabilities
from talktoharnesses.providers.compatibility import (
    CompatibilityFloor,
    LatestVerified,
    MatrixMode,
    ReleaseCapabilities,
    assert_supported_platform,
    compare_dotted,
    enforce_operation,
    validate_floor_document,
)

_COMPAT = ConfigDict(extra="forbid", frozen=True)


class CodexFloor(CompatibilityFloor):
    sdk_version: str
    runtime_package: str
    runtime_version: str
    notes: str | None = None


class CodexReleaseRecord(BaseModel):
    model_config = _COMPAT

    id: str
    sdk_version: str
    runtime_package: str
    runtime_version: str
    platforms: list[str] = Field(default_factory=list)
    capabilities: ReleaseCapabilities = Field(default_factory=ReleaseCapabilities)
    notes: str | None = None

    def to_harness_capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            kind=HarnessKind.CODEX,
            version=self.sdk_version,
            supports_steer=self.capabilities.supports_steer,
            supports_resume=self.capabilities.supports_resume,
            supports_interrupt=self.capabilities.supports_interrupt,
            supports_multi_interaction=self.capabilities.supports_multi_interaction,
            supports_nested_activity=self.capabilities.supports_nested_activity,
        )


class CodexCompatibilityDoc(BaseModel):
    model_config = _COMPAT

    adapter_version: str
    floor: CodexFloor
    latest_verified: LatestVerified | None = None


@lru_cache(maxsize=1)
def load_codex_compatibility() -> CodexCompatibilityDoc:
    root = resources.files("talktoharnesses.data.compatibility")
    data = (root / "codex.json").read_text(encoding="utf-8")
    doc = CodexCompatibilityDoc.model_validate(json.loads(data))
    validate_floor_document(doc, harness_label="codex", compare=compare_dotted)
    return doc


def match_release(
    *,
    sdk_version: str,
    runtime_version: str,
    platform: str | None = None,
) -> CodexReleaseRecord:
    plat = platform or sys.platform
    doc = load_codex_compatibility()
    floor = doc.floor
    assert_supported_platform(plat, floor.platforms, harness_label="codex")
    if sdk_version != floor.sdk_version or runtime_version != floor.runtime_version:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "unknown codex release",
            details={
                "sdk_version": sdk_version,
                "runtime_version": runtime_version,
                "floor_sdk_version": floor.sdk_version,
                "floor_runtime_version": floor.runtime_version,
            },
        )
    return CodexReleaseRecord(
        id=f"codex-openai-codex-{sdk_version}",
        sdk_version=sdk_version,
        runtime_package=floor.runtime_package,
        runtime_version=runtime_version,
        platforms=list(floor.platforms),
        capabilities=floor.capabilities,
        notes=floor.notes,
    )


def enforce_published_operation(
    release: CodexReleaseRecord,
    *,
    mode: MatrixMode,
    platform: str | None = None,
    enforce_published: bool = True,
) -> None:
    enforce_operation(
        release.capabilities,
        mode=mode,
        platforms=release.platforms,
        harness_label="codex",
        platform=platform,
        enforce=enforce_published,
    )


class CodexCompatibilitySection:
    def __init__(self, doc: CodexCompatibilityDoc | None = None) -> None:
        self._doc = doc or load_codex_compatibility()

    @property
    def kind(self) -> HarnessKind:
        return HarnessKind.CODEX

    @property
    def adapter_version(self) -> str:
        return self._doc.adapter_version

    @property
    def floor_label(self) -> str:
        floor = self._doc.floor
        return (
            f"SDK `{floor.sdk_version}` + `{floor.runtime_package}` "
            f"`{floor.runtime_version}` (exact)"
        )

    @property
    def platforms(self) -> list[str]:
        return list(self._doc.floor.platforms)

    @property
    def capabilities(self) -> ReleaseCapabilities:
        return self._doc.floor.capabilities

    @property
    def latest_verified(self) -> LatestVerified | None:
        return self._doc.latest_verified

    def render_extra_floor_lines(self) -> list[str]:
        return []

    def render_extra_notes(self) -> list[str]:
        notes = self._doc.floor.notes
        if not notes:
            return []
        return ["### Notes", "", f"- {notes}"]


def codex_compatibility_section() -> CodexCompatibilitySection:
    return CodexCompatibilitySection()
