"""Strict OpenCode compatibility source."""

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


class OpenCodeCompatibilityDoc(SharedMatrices):
    model_config = _COMPAT

    adapter_version: str
    releases: list[OpenCodeReleaseRecord] = Field(default_factory=list[OpenCodeReleaseRecord])


@lru_cache(maxsize=1)
def load_opencode_compatibility() -> OpenCodeCompatibilityDoc:
    root = resources.files("talktoharnesses.data.compatibility")
    data = (root / "opencode.json").read_text(encoding="utf-8")
    doc = OpenCodeCompatibilityDoc.model_validate(json.loads(data))
    validate_matrices(
        releases=doc.releases,
        matrices=doc.as_mapping(),
        harness_label="opencode",
    )
    return doc


def parse_version_stdout(version_stdout: str) -> str:
    complete = version_stdout.strip()
    if not complete or "\n" in complete:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "malformed opencode version output",
            details={"version_stdout": complete},
        )
    # Accept "1.2.27" or "opencode 1.2.27"
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
    for release in doc.releases:
        if release.cli_version == version or release.version_stdout_prefix == version:
            if release.platforms and plat not in release.platforms:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "opencode release not supported on this platform",
                    details={
                        "release_id": release.id,
                        "platform": plat,
                        "supported_platforms": list(release.platforms),
                    },
                )
            return release
    raise DomainError(
        ErrorCode.PROVIDER_INCOMPATIBLE,
        "unknown opencode release",
        details={
            "cli_version": version,
            "known_releases": [r.id for r in doc.releases],
        },
    )


def enforce_published_operation(
    release: OpenCodeReleaseRecord,
    *,
    mode: MatrixMode,
    platform: str | None = None,
    enforce_published: bool = True,
) -> None:
    enforce_doc_operation(
        load_opencode_compatibility(),
        release.id,
        mode=mode,
        harness_label="opencode",
        platform=platform,
        enforce_published=enforce_published,
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

    def matrix(self, mode: MatrixMode) -> list[CompatibilityMatrixEntry]:
        return list(getattr(self._doc, f"{mode}_matrix"))

    def render_release_rows(self) -> list[str]:
        doc = self._doc
        if not doc.releases:
            return []
        lines = [
            f"| Release ID | CLI version | Platforms | {CAPABILITY_TABLE_HEADER} |",
            f"| --- | --- | --- | {CAPABILITY_TABLE_DIVIDER} |",
        ]
        for release in doc.releases:
            platforms = ", ".join(release.platforms) if release.platforms else "—"
            caps = release.capabilities
            lines.append(
                f"| `{release.id}` | {release.cli_version} | {platforms} | "
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


def opencode_compatibility_section() -> OpenCodeCompatibilitySection:
    return OpenCodeCompatibilitySection()
