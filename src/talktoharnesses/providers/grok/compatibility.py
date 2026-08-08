"""Strict Grok compatibility source and SUPPORTED_HARNESSES rendering."""

from __future__ import annotations

import json
import re
import sys
from functools import lru_cache
from importlib import resources
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import HarnessCapabilities

_COMPAT = ConfigDict(extra="forbid", frozen=True)


class GrokReleaseCapabilities(BaseModel):
    model_config = _COMPAT

    supports_resume: bool = False
    supports_interrupt: bool = True
    supports_steer: bool = False
    supports_multi_interaction: bool = False
    supports_nested_activity: bool = False


class GrokReleaseRecord(BaseModel):
    model_config = _COMPAT

    id: str
    cli_version: str
    cli_build: str
    cli_channel: str = "stable"
    version_stdout_prefix: str
    agent_name: str
    acp_protocol_version: int = 1
    platforms: list[str] = Field(default_factory=list)
    capabilities: GrokReleaseCapabilities = Field(default_factory=GrokReleaseCapabilities)
    required_agent_methods: list[str] = Field(default_factory=list)
    allowlisted_extensions: list[str] = Field(default_factory=list)
    notes: str | None = None

    def to_harness_capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            kind=HarnessKind.GROK,
            version=f"{self.cli_version} ({self.cli_build}) [{self.cli_channel}]",
            supports_steer=self.capabilities.supports_steer,
            supports_resume=self.capabilities.supports_resume,
            supports_interrupt=self.capabilities.supports_interrupt,
            supports_multi_interaction=self.capabilities.supports_multi_interaction,
            supports_nested_activity=self.capabilities.supports_nested_activity,
        )


class GrokCompatibilityDoc(BaseModel):
    model_config = _COMPAT

    adapter_version: str
    releases: list[GrokReleaseRecord] = Field(default_factory=list[GrokReleaseRecord])
    create_matrix: list[str] = Field(default_factory=list[str])
    resume_matrix: list[str] = Field(default_factory=list[str])

    def release_by_id(self, release_id: str) -> GrokReleaseRecord | None:
        for release in self.releases:
            if release.id == release_id:
                return release
        return None


_VERSION_RE = re.compile(
    r"^grok\s+(?P<version>\d+\.\d+\.\d+)\s+\((?P<build>[0-9a-fA-F]+)\)"
    r"(?:\s+\[(?P<channel>[^\]]+)\])?$",
)


@lru_cache(maxsize=1)
def load_grok_compatibility() -> GrokCompatibilityDoc:
    """Load the packaged Grok compatibility document."""
    root = resources.files("talktoharnesses.data.compatibility")
    data = (root / "grok.json").read_text(encoding="utf-8")
    return GrokCompatibilityDoc.model_validate(json.loads(data))


def parse_version_stdout(version_stdout: str) -> tuple[str, str, str]:
    """Parse ``grok --version`` stdout into (version, build, channel)."""
    complete = version_stdout.strip()
    match = _VERSION_RE.fullmatch(complete)
    if match is None:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "malformed grok version output",
            details={"version_stdout": complete},
        )
    channel = match.group("channel") or "stable"
    return match.group("version"), match.group("build"), channel


def match_release(
    version_stdout: str,
    *,
    platform: str | None = None,
) -> GrokReleaseRecord:
    """Match version stdout to a known release; fail hard if unknown."""
    version, build, channel = parse_version_stdout(version_stdout)
    plat = platform or sys.platform
    doc = load_grok_compatibility()
    for release in doc.releases:
        if (
            release.cli_version == version
            and release.cli_build == build
            and release.cli_channel == channel
        ):
            if release.platforms and plat not in release.platforms:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "grok release not supported on this platform",
                    details={
                        "release_id": release.id,
                        "platform": plat,
                        "supported_platforms": list(release.platforms),
                    },
                )
            accepted_outputs = {
                release.version_stdout_prefix,
                f"{release.version_stdout_prefix} [{release.cli_channel}]",
            }
            if version_stdout.strip() not in accepted_outputs:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "grok version output does not match compatibility record",
                    details={"version_stdout": version_stdout.strip(), "release_id": release.id},
                )
            return release
    raise DomainError(
        ErrorCode.PROVIDER_INCOMPATIBLE,
        "unknown grok release",
        details={
            "cli_version": version,
            "cli_build": build,
            "cli_channel": channel,
            "known_releases": [r.id for r in doc.releases],
        },
    )


def assert_matrix_membership(
    release: GrokReleaseRecord,
    *,
    mode: Literal["create", "resume"],
    enforce_published: bool = True,
) -> None:
    """Fail when a published matrix is enforced and the release is absent."""
    if not enforce_published:
        return
    doc = load_grok_compatibility()
    matrix = doc.create_matrix if mode == "create" else doc.resume_matrix
    if not matrix:
        # Empty published matrix: allow development against release records
        # (fixtures / unreleased work). Live gates populate matrices later.
        return
    if release.id not in matrix:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            f"grok release not in published {mode} matrix",
            details={"release_id": release.id, "matrix": list(matrix)},
        )


def render_supported_harnesses_markdown(doc: GrokCompatibilityDoc | None = None) -> str:
    """Generate the Grok section of SUPPORTED_HARNESSES.md from the same source."""
    doc = doc or load_grok_compatibility()
    lines = [
        "# Supported Harnesses",
        "",
        "This document is generated from packaged compatibility data.",
        "Do not edit the Grok section by hand; regenerate via",
        "`python -m talktoharnesses.providers.grok.render_supported`.",
        "",
        "## Grok",
        "",
        f"- Adapter version: `{doc.adapter_version}`",
        "",
        "### Known releases (implementation targets)",
        "",
    ]
    if not doc.releases:
        lines.append("_No releases recorded._")
        lines.append("")
    else:
        lines.extend(
            [
                "| Release ID | CLI version | Build | ACP | Platforms | Resume | Steer |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for release in doc.releases:
            platforms = ", ".join(release.platforms) if release.platforms else "—"
            caps = release.capabilities
            lines.append(
                f"| `{release.id}` | {release.cli_version} | `{release.cli_build}` | "
                f"v{release.acp_protocol_version} | {platforms} | "
                f"{'yes' if caps.supports_resume else 'no'} | "
                f"{'yes' if caps.supports_steer else 'no'} |"
            )
        lines.append("")

    lines.append("### Published create matrix")
    lines.append("")
    if not doc.create_matrix:
        lines.append(
            "_No published create combinations yet. "
            "Create rows are added only after the opt-in create suite passes._"
        )
    else:
        for release_id in doc.create_matrix:
            lines.append(f"- `{release_id}`")
    lines.append("")
    lines.append("### Published resume matrix")
    lines.append("")
    if not doc.resume_matrix:
        lines.append(
            "_No published resume combinations yet. "
            "Resume rows are added only after the opt-in resume suite passes._"
        )
    else:
        for release_id in doc.resume_matrix:
            lines.append(f"- `{release_id}`")
    lines.append("")
    return "\n".join(lines)
