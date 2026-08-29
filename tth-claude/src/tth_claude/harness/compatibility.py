"""Claude Code compatibility floor and probed-identity matching."""

from __future__ import annotations

import json
import sys
from functools import lru_cache
from importlib import resources
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError
from tth_types.harness import HarnessCapabilities

from tth_claude.shared.compatibility import (
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


class ClaudeFloor(CompatibilityFloor):
    sdk_version: str
    notes: str | None = None


class ClaudeReleaseRecord(BaseModel):
    model_config = _COMPAT

    id: str
    sdk_version: str
    cli_version: str
    cli_source: Literal["bundled", "explicit"] = "bundled"
    platforms: list[str] = Field(default_factory=list)
    capabilities: ReleaseCapabilities = Field(default_factory=ReleaseCapabilities)
    notes: str | None = None

    def to_harness_capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            kind=HarnessKind.CLAUDE,
            version=f"{self.sdk_version}+cli-{self.cli_version}",
            supports_steer=self.capabilities.supports_steer,
            supports_resume=self.capabilities.supports_resume,
            supports_interrupt=self.capabilities.supports_interrupt,
            supports_multi_interaction=self.capabilities.supports_multi_interaction,
            supports_nested_activity=self.capabilities.supports_nested_activity,
        )


class ClaudeCompatibilityDoc(BaseModel):
    model_config = _COMPAT

    adapter_version: str
    floor: ClaudeFloor
    latest_verified: LatestVerified | None = None


@lru_cache(maxsize=1)
def load_claude_compatibility() -> ClaudeCompatibilityDoc:
    root = resources.files("tth_claude.data.compatibility")
    data = (root / "claude.json").read_text(encoding="utf-8")
    doc = ClaudeCompatibilityDoc.model_validate(json.loads(data))
    validate_floor_document(doc, harness_label="claude", compare=compare_dotted)
    return doc


def match_release(
    *,
    sdk_version: str,
    cli_version: str,
    cli_source: Literal["bundled", "explicit"] = "bundled",
    platform: str | None = None,
) -> ClaudeReleaseRecord:
    plat = platform or sys.platform
    doc = load_claude_compatibility()
    floor = doc.floor
    assert_supported_platform(plat, floor.platforms, harness_label="claude")
    if sdk_version != floor.sdk_version:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "unknown claude release",
            details={
                "sdk_version": sdk_version,
                "cli_version": cli_version,
                "cli_source": cli_source,
                "floor_sdk_version": floor.sdk_version,
            },
        )
    reject_below_floor(
        probed=cli_version,
        floor=floor.version,
        compare=compare_dotted,
        harness_label="claude",
        details={
            "sdk_version": sdk_version,
            "cli_version": cli_version,
            "cli_source": cli_source,
        },
    )
    return ClaudeReleaseRecord(
        id=f"claude-agent-sdk-{sdk_version}-{cli_source}-{cli_version}",
        sdk_version=sdk_version,
        cli_version=cli_version,
        cli_source=cli_source,
        platforms=list(floor.platforms),
        capabilities=floor.capabilities,
        notes=floor.notes,
    )


def enforce_published_operation(
    release: ClaudeReleaseRecord,
    *,
    mode: MatrixMode,
    platform: str | None = None,
    enforce_published: bool = True,
) -> None:
    enforce_operation(
        release.capabilities,
        mode=mode,
        platforms=release.platforms,
        harness_label="claude",
        platform=platform,
        enforce=enforce_published,
    )
