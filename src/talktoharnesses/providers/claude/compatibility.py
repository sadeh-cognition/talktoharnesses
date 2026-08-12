"""Strict Claude Code compatibility source."""

from __future__ import annotations

import json
import sys
from functools import lru_cache
from importlib import resources
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import HarnessCapabilities
from talktoharnesses.providers.compatibility import (
    CAPABILITY_TABLE_DIVIDER,
    CAPABILITY_TABLE_HEADER,
    CompatibilityMatrixEntry,
    MatrixMode,
    ReleaseCapabilities,
    SharedMatrices,
    capability_cells,
    enforce_doc_operation,
    validate_matrices,
)

_COMPAT = ConfigDict(extra="forbid", frozen=True)


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


class ClaudeCompatibilityDoc(SharedMatrices):
    model_config = _COMPAT

    adapter_version: str
    releases: list[ClaudeReleaseRecord] = Field(default_factory=list[ClaudeReleaseRecord])


@lru_cache(maxsize=1)
def load_claude_compatibility() -> ClaudeCompatibilityDoc:
    root = resources.files("talktoharnesses.data.compatibility")
    data = (root / "claude.json").read_text(encoding="utf-8")
    doc = ClaudeCompatibilityDoc.model_validate(json.loads(data))
    validate_matrices(
        releases=doc.releases,
        matrices=doc.as_mapping(),
        harness_label="claude",
    )
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
    for release in doc.releases:
        if (
            release.sdk_version == sdk_version
            and release.cli_version == cli_version
            and release.cli_source == cli_source
        ):
            if release.platforms and plat not in release.platforms:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "claude release not supported on this platform",
                    details={
                        "release_id": release.id,
                        "platform": plat,
                        "supported_platforms": list(release.platforms),
                    },
                )
            return release
    raise DomainError(
        ErrorCode.PROVIDER_INCOMPATIBLE,
        "unknown claude release",
        details={
            "sdk_version": sdk_version,
            "cli_version": cli_version,
            "cli_source": cli_source,
            "known_releases": [r.id for r in doc.releases],
        },
    )


def enforce_published_operation(
    release: ClaudeReleaseRecord,
    *,
    mode: MatrixMode,
    platform: str | None = None,
    enforce_published: bool = True,
) -> None:
    enforce_doc_operation(
        load_claude_compatibility(),
        release.id,
        mode=mode,
        harness_label="claude",
        platform=platform,
        enforce_published=enforce_published,
    )


class ClaudeCompatibilitySection:
    def __init__(self, doc: ClaudeCompatibilityDoc | None = None) -> None:
        self._doc = doc or load_claude_compatibility()

    @property
    def kind(self) -> HarnessKind:
        return HarnessKind.CLAUDE

    @property
    def adapter_version(self) -> str:
        return self._doc.adapter_version

    def matrix(self, mode: MatrixMode) -> list[CompatibilityMatrixEntry]:
        return list(getattr(self._doc, f"{mode}_matrix"))

    def render_release_rows(self) -> list[str]:
        doc = self._doc
        if not doc.releases:
            return []
        lines = [
            f"| Release ID | SDK | CLI | Source | Platforms | {CAPABILITY_TABLE_HEADER} |",
            f"| --- | --- | --- | --- | --- | {CAPABILITY_TABLE_DIVIDER} |",
        ]
        for release in doc.releases:
            platforms = ", ".join(release.platforms) if release.platforms else "—"
            caps = release.capabilities
            lines.append(
                f"| `{release.id}` | {release.sdk_version} | {release.cli_version} | "
                f"{release.cli_source} | {platforms} | "
                f"{capability_cells(caps)} |"
            )
        return lines

    def render_extra_notes(self) -> list[str]:
        notes: list[str] = []
        for release in self._doc.releases:
            if release.notes:
                notes.append(f"- `{release.id}`: {release.notes}")
        if notes:
            return ["### Notes", ""] + notes
        return []


def claude_compatibility_section() -> ClaudeCompatibilitySection:
    return ClaudeCompatibilitySection()
