"""OpenCode compatibility floor and probed-identity matching."""

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
    reject_below_floor,
    validate_floor_document,
)

_COMPAT = ConfigDict(extra="forbid", frozen=True)


class OpenCodeFloor(CompatibilityFloor):
    notes: str | None = None


class OpenCodeReleaseRecord(BaseModel):
    model_config = _COMPAT

    id: str
    cli_version: str
    version_stdout_prefix: str
    platforms: list[str] = Field(default_factory=list)
    capabilities: ReleaseCapabilities = Field(default_factory=ReleaseCapabilities)
    notes: str | None = None

    def to_harness_capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            kind=HarnessKind.OPENCODE,
            version=self.cli_version,
            supports_steer=self.capabilities.supports_steer,
            supports_resume=self.capabilities.supports_resume,
            supports_interrupt=self.capabilities.supports_interrupt,
            supports_multi_interaction=self.capabilities.supports_multi_interaction,
            supports_nested_activity=self.capabilities.supports_nested_activity,
        )


class OpenCodeCompatibilityDoc(BaseModel):
    model_config = _COMPAT

    adapter_version: str
    floor: OpenCodeFloor
    latest_verified: LatestVerified | None = None


@lru_cache(maxsize=1)
def load_opencode_compatibility() -> OpenCodeCompatibilityDoc:
    root = resources.files("talktoharnesses.data.compatibility")
    data = (root / "opencode.json").read_text(encoding="utf-8")
    doc = OpenCodeCompatibilityDoc.model_validate(json.loads(data))
    validate_floor_document(doc, harness_label="opencode", compare=compare_dotted)
    return doc


def parse_version_stdout(version_stdout: str) -> str:
    complete = version_stdout.strip()
    if not complete or "\n" in complete:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "malformed opencode version output",
            details={"version_stdout": complete},
        )
    if complete.startswith("opencode "):
        complete = complete.removeprefix("opencode ").strip()
    return complete


def match_release(
    version_stdout: str,
    *,
    platform: str | None = None,
) -> OpenCodeReleaseRecord:
    version = parse_version_stdout(version_stdout)
    plat = platform or sys.platform
    doc = load_opencode_compatibility()
    floor = doc.floor
    assert_supported_platform(plat, floor.platforms, harness_label="opencode")
    reject_below_floor(
        probed=version,
        floor=floor.version,
        compare=compare_dotted,
        harness_label="opencode",
        details={"cli_version": version},
    )
    return OpenCodeReleaseRecord(
        id=f"opencode-{version}",
        cli_version=version,
        version_stdout_prefix=version,
        platforms=list(floor.platforms),
        capabilities=floor.capabilities,
        notes=floor.notes,
    )


def enforce_published_operation(
    release: OpenCodeReleaseRecord,
    *,
    mode: MatrixMode,
    platform: str | None = None,
    enforce_published: bool = True,
) -> None:
    enforce_operation(
        release.capabilities,
        mode=mode,
        platforms=release.platforms,
        harness_label="opencode",
        platform=platform,
        enforce=enforce_published,
    )


class OpenCodeCompatibilitySection:
    def __init__(self, doc: OpenCodeCompatibilityDoc | None = None) -> None:
        self._doc = doc or load_opencode_compatibility()

    @property
    def kind(self) -> HarnessKind:
        return HarnessKind.OPENCODE

    @property
    def adapter_version(self) -> str:
        return self._doc.adapter_version

    @property
    def floor_label(self) -> str:
        return f"CLI `>= {self._doc.floor.version}`"

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


def opencode_compatibility_section() -> OpenCodeCompatibilitySection:
    return OpenCodeCompatibilitySection()
