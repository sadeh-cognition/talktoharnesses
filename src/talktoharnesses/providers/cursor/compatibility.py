"""Strict Cursor compatibility source."""

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
    CompatibilityMatrixEntry,
    ReleaseCapabilities,
    assert_matrix_membership,
    validate_matrices,
    yes_no,
)

_COMPAT = ConfigDict(extra="forbid", frozen=True)


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
    releases: list[CursorReleaseRecord] = Field(default_factory=list[CursorReleaseRecord])
    create_matrix: list[CompatibilityMatrixEntry] = Field(
        default_factory=list[CompatibilityMatrixEntry]
    )
    resume_matrix: list[CompatibilityMatrixEntry] = Field(
        default_factory=list[CompatibilityMatrixEntry]
    )

    def release_by_id(self, release_id: str) -> CursorReleaseRecord | None:
        for release in self.releases:
            if release.id == release_id:
                return release
        return None


@lru_cache(maxsize=1)
def load_cursor_compatibility() -> CursorCompatibilityDoc:
    root = resources.files("talktoharnesses.data.compatibility")
    data = (root / "cursor.json").read_text(encoding="utf-8")
    doc = CursorCompatibilityDoc.model_validate(json.loads(data))
    validate_matrices(
        releases=doc.releases,
        create_matrix=doc.create_matrix,
        resume_matrix=doc.resume_matrix,
        harness_label="cursor",
    )
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
    for release in doc.releases:
        if release.cli_version == version or release.version_stdout_prefix == version:
            if release.platforms and plat not in release.platforms:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "cursor release not supported on this platform",
                    details={
                        "release_id": release.id,
                        "platform": plat,
                        "supported_platforms": list(release.platforms),
                    },
                )
            if version_stdout.strip() != release.version_stdout_prefix:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "cursor version output does not match compatibility record",
                    details={"version_stdout": version_stdout.strip(), "release_id": release.id},
                )
            return release
    raise DomainError(
        ErrorCode.PROVIDER_INCOMPATIBLE,
        "unknown cursor release",
        details={
            "cli_version": version,
            "known_releases": [r.id for r in doc.releases],
        },
    )


def enforce_published_operation(
    release: CursorReleaseRecord,
    *,
    mode: Literal["create", "resume"],
    platform: str | None = None,
    enforce_published: bool = True,
) -> None:
    doc = load_cursor_compatibility()
    matrix = doc.create_matrix if mode == "create" else doc.resume_matrix
    assert_matrix_membership(
        release_id=release.id,
        platform=platform or sys.platform,
        matrix=matrix,
        mode=mode,
        harness_label="cursor",
        enforce_published=enforce_published,
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
    def create_matrix(self) -> list[CompatibilityMatrixEntry]:
        return list(self._doc.create_matrix)

    @property
    def resume_matrix(self) -> list[CompatibilityMatrixEntry]:
        return list(self._doc.resume_matrix)

    def render_release_rows(self) -> list[str]:
        doc = self._doc
        if not doc.releases:
            return []
        lines = [
            "| Release ID | CLI version | ACP | Platforms | Resume | Steer |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for release in doc.releases:
            platforms = ", ".join(release.platforms) if release.platforms else "—"
            caps = release.capabilities
            lines.append(
                f"| `{release.id}` | {release.cli_version} | "
                f"v{release.acp_protocol_version} | {platforms} | "
                f"{yes_no(caps.supports_resume)} | "
                f"{yes_no(caps.supports_steer)} |"
            )
        return lines

    def render_extra_notes(self) -> list[str]:
        return []


def cursor_compatibility_section() -> CursorCompatibilitySection:
    return CursorCompatibilitySection()
