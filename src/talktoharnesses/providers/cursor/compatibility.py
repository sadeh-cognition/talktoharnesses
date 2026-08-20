"""Cursor compatibility floor and probed-identity matching."""

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
    compare_cursor_date,
    enforce_operation,
    reject_below_floor,
    validate_floor_document,
)

_COMPAT = ConfigDict(extra="forbid", frozen=True)


class CursorFloor(CompatibilityFloor):
    agent_name: str
    acp_protocol_version: int = 1
    required_agent_methods: list[str] = Field(default_factory=list)
    allowlisted_extensions: list[str] = Field(default_factory=list)
    notes: str | None = None


class CursorReleaseRecord(BaseModel):
    model_config = _COMPAT

    id: str
    cli_version: str
    version_stdout_prefix: str
    agent_name: str
    acp_protocol_version: int = 1
    platforms: list[str] = Field(default_factory=list)
    capabilities: ReleaseCapabilities = Field(default_factory=ReleaseCapabilities)
    required_agent_methods: list[str] = Field(default_factory=list)
    allowlisted_extensions: list[str] = Field(default_factory=list)
    notes: str | None = None

    def to_harness_capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            kind=HarnessKind.CURSOR,
            version=self.cli_version,
            supports_steer=self.capabilities.supports_steer,
            supports_resume=self.capabilities.supports_resume,
            supports_interrupt=self.capabilities.supports_interrupt,
            supports_multi_interaction=self.capabilities.supports_multi_interaction,
            supports_nested_activity=self.capabilities.supports_nested_activity,
        )


class CursorCompatibilityDoc(BaseModel):
    model_config = _COMPAT

    adapter_version: str
    floor: CursorFloor
    latest_verified: LatestVerified | None = None


@lru_cache(maxsize=1)
def load_cursor_compatibility() -> CursorCompatibilityDoc:
    root = resources.files("talktoharnesses.data.compatibility")
    data = (root / "cursor.json").read_text(encoding="utf-8")
    doc = CursorCompatibilityDoc.model_validate(json.loads(data))
    validate_floor_document(doc, harness_label="cursor", compare=compare_cursor_date)
    return doc


def parse_version_stdout(version_stdout: str) -> str:
    complete = version_stdout.strip()
    if not complete or "\n" in complete:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "malformed cursor version output",
            details={"version_stdout": complete},
        )
    return complete


def match_release(
    version_stdout: str,
    *,
    platform: str | None = None,
) -> CursorReleaseRecord:
    version = parse_version_stdout(version_stdout)
    plat = platform or sys.platform
    doc = load_cursor_compatibility()
    floor = doc.floor
    assert_supported_platform(plat, floor.platforms, harness_label="cursor")
    reject_below_floor(
        probed=version,
        floor=floor.version,
        compare=compare_cursor_date,
        harness_label="cursor",
        details={"cli_version": version},
    )
    return CursorReleaseRecord(
        id=f"cursor-{version}",
        cli_version=version,
        version_stdout_prefix=version,
        agent_name=floor.agent_name,
        acp_protocol_version=floor.acp_protocol_version,
        platforms=list(floor.platforms),
        capabilities=floor.capabilities,
        required_agent_methods=list(floor.required_agent_methods),
        allowlisted_extensions=list(floor.allowlisted_extensions),
        notes=floor.notes,
    )


def enforce_published_operation(
    release: CursorReleaseRecord,
    *,
    mode: MatrixMode,
    platform: str | None = None,
    enforce_published: bool = True,
) -> None:
    enforce_operation(
        release.capabilities,
        mode=mode,
        platforms=release.platforms,
        harness_label="cursor",
        platform=platform,
        enforce=enforce_published,
    )


class CursorCompatibilitySection:
    def __init__(self, doc: CursorCompatibilityDoc | None = None) -> None:
        self._doc = doc or load_cursor_compatibility()

    @property
    def kind(self) -> HarnessKind:
        return HarnessKind.CURSOR

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
        return [f"- ACP: v{self._doc.floor.acp_protocol_version}"]

    def render_extra_notes(self) -> list[str]:
        notes = self._doc.floor.notes
        if not notes:
            return []
        return ["### Notes", "", f"- {notes}"]


def cursor_compatibility_section() -> CursorCompatibilitySection:
    return CursorCompatibilitySection()
