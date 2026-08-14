"""Strict Grok compatibility source."""

from __future__ import annotations

import json
import re
import sys
from functools import lru_cache
from importlib import resources

from pydantic import BaseModel, ConfigDict, Field

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import HarnessCapabilities, HarnessEffortInfo
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

# Backward-compatible alias used by existing imports/tests.
GrokReleaseCapabilities = ReleaseCapabilities


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
    capabilities: ReleaseCapabilities = Field(default_factory=ReleaseCapabilities)
    required_agent_methods: list[str] = Field(default_factory=list)
    allowlisted_extensions: list[str] = Field(default_factory=list)
    effort_levels: list[str] = Field(default_factory=list)
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
            efforts=tuple(
                HarnessEffortInfo(id=level, label=level.title())
                for level in self.effort_levels
            ),
        )


class GrokCompatibilityDoc(SharedMatrices):
    model_config = _COMPAT

    adapter_version: str
    releases: list[GrokReleaseRecord] = Field(default_factory=list[GrokReleaseRecord])

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
    doc = GrokCompatibilityDoc.model_validate(json.loads(data))
    validate_matrices(
        releases=doc.releases,
        matrices=doc.as_mapping(),
        harness_label="grok",
    )
    return doc


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
            "provider": "Grok",
            "installed_version": f"{version} ({build}) [{channel}]",
            "supported_versions": [
                f"{release.cli_version} ({release.cli_build}) [{release.cli_channel}]"
                for release in doc.releases
                if not release.platforms or plat in release.platforms
            ],
            "cli_version": version,
            "cli_build": build,
            "cli_channel": channel,
            "known_releases": [r.id for r in doc.releases],
        },
    )


def enforce_published_operation(
    release: GrokReleaseRecord,
    *,
    mode: MatrixMode,
    platform: str | None = None,
    enforce_published: bool = True,
) -> None:
    """Validate the probed release against the published operation matrix."""
    enforce_doc_operation(
        load_grok_compatibility(),
        release.id,
        mode=mode,
        harness_label="grok",
        platform=platform,
        enforce_published=enforce_published,
    )


class GrokCompatibilitySection:
    """CompatibilitySection adapter for the shared Markdown renderer."""

    def __init__(self, doc: GrokCompatibilityDoc | None = None) -> None:
        self._doc = doc or load_grok_compatibility()

    @property
    def kind(self) -> HarnessKind:
        return HarnessKind.GROK

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
            f"| Release ID | CLI version | Build | ACP | Platforms | {CAPABILITY_TABLE_HEADER} |",
            f"| --- | --- | --- | --- | --- | {CAPABILITY_TABLE_DIVIDER} |",
        ]
        for release in doc.releases:
            platforms = ", ".join(release.platforms) if release.platforms else "—"
            caps = release.capabilities
            lines.append(
                f"| `{release.id}` | {release.cli_version} | `{release.cli_build}` | "
                f"v{release.acp_protocol_version} | {platforms} | "
                f"{capability_cells(caps)} |"
            )
        return lines

    def render_extra_notes(self) -> list[str]:
        return []


def grok_compatibility_section() -> GrokCompatibilitySection:
    return GrokCompatibilitySection()


def render_supported_harnesses_markdown(doc: GrokCompatibilityDoc | None = None) -> str:
    """Backward-compatible Grok-only render; prefer shared renderer."""
    from talktoharnesses.providers.compatibility import (
        render_supported_harnesses_markdown as render_all,
    )

    if doc is None:
        return render_all()
    return render_all([GrokCompatibilitySection(doc)])
