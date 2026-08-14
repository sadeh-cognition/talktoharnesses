"""Strict Prime Agent compatibility source."""

from __future__ import annotations

import json
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
from talktoharnesses.providers.prime_agent.argv import THINKING_LEVELS

_COMPAT = ConfigDict(extra="forbid", frozen=True)


class PrimeAgentReleaseRecord(BaseModel):
    model_config = _COMPAT

    id: str
    cli_version: str
    platforms: list[str] = Field(default_factory=list)
    capabilities: ReleaseCapabilities = Field(default_factory=ReleaseCapabilities)
    notes: str | None = None

    def to_harness_capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            kind=HarnessKind.PRIME_AGENT,
            version=self.cli_version,
            supports_steer=self.capabilities.supports_steer,
            supports_resume=self.capabilities.supports_resume,
            supports_interrupt=self.capabilities.supports_interrupt,
            supports_multi_interaction=self.capabilities.supports_multi_interaction,
            supports_nested_activity=self.capabilities.supports_nested_activity,
            efforts=tuple(
                HarnessEffortInfo(id=level, label=level.title()) for level in THINKING_LEVELS
            ),
        )


class PrimeAgentCompatibilityDoc(SharedMatrices):
    model_config = _COMPAT

    adapter_version: str
    releases: list[PrimeAgentReleaseRecord] = Field(default_factory=list[PrimeAgentReleaseRecord])


@lru_cache(maxsize=1)
def load_prime_agent_compatibility() -> PrimeAgentCompatibilityDoc:
    root = resources.files("talktoharnesses.data.compatibility")
    data = (root / "prime_agent.json").read_text(encoding="utf-8")
    doc = PrimeAgentCompatibilityDoc.model_validate(json.loads(data))
    validate_matrices(
        releases=doc.releases,
        matrices=doc.as_mapping(),
        harness_label="prime_agent",
    )
    return doc


def parse_version_stdout(version_stdout: str) -> str:
    version = version_stdout.strip()
    if not version or "\n" in version:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "malformed prime-agent version output",
            details={"version_stdout": version},
        )
    if version.startswith("prime-agent "):
        version = version.removeprefix("prime-agent ").strip()
    return version.removeprefix("v")


def match_release(
    version_stdout: str,
    *,
    platform: str | None = None,
) -> PrimeAgentReleaseRecord:
    version = parse_version_stdout(version_stdout)
    selected_platform = platform or sys.platform
    doc = load_prime_agent_compatibility()
    for release in doc.releases:
        if release.cli_version == version:
            if release.platforms and selected_platform not in release.platforms:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "prime-agent release not supported on this platform",
                    details={
                        "release_id": release.id,
                        "platform": selected_platform,
                        "supported_platforms": list(release.platforms),
                    },
                )
            return release
    raise DomainError(
        ErrorCode.PROVIDER_INCOMPATIBLE,
        "unknown prime-agent release",
        details={
            "cli_version": version,
            "known_releases": [release.id for release in doc.releases],
        },
    )


def enforce_published_operation(
    release: PrimeAgentReleaseRecord,
    *,
    mode: MatrixMode,
    platform: str | None = None,
    enforce_published: bool = True,
) -> None:
    enforce_doc_operation(
        load_prime_agent_compatibility(),
        release.id,
        mode=mode,
        harness_label="prime_agent",
        platform=platform,
        enforce_published=enforce_published,
    )


class PrimeAgentCompatibilitySection:
    def __init__(self, doc: PrimeAgentCompatibilityDoc | None = None) -> None:
        self._doc = doc or load_prime_agent_compatibility()

    @property
    def kind(self) -> HarnessKind:
        return HarnessKind.PRIME_AGENT

    @property
    def adapter_version(self) -> str:
        return self._doc.adapter_version

    def matrix(self, mode: MatrixMode) -> list[CompatibilityMatrixEntry]:
        return list(getattr(self._doc, f"{mode}_matrix"))

    def render_release_rows(self) -> list[str]:
        if not self._doc.releases:
            return []
        lines = [
            f"| Release ID | CLI version | Transport | Platforms | {CAPABILITY_TABLE_HEADER} |",
            f"| --- | --- | --- | --- | {CAPABILITY_TABLE_DIVIDER} |",
        ]
        for release in self._doc.releases:
            platforms = ", ".join(release.platforms) if release.platforms else "—"
            lines.append(
                f"| `{release.id}` | {release.cli_version} | JSONL RPC | {platforms} | "
                f"{capability_cells(release.capabilities)} |"
            )
        return lines

    def render_extra_notes(self) -> list[str]:
        notes = [
            f"- `{release.id}`: {release.notes}" for release in self._doc.releases if release.notes
        ]
        return ["### Notes", "", *notes] if notes else []


def prime_agent_compatibility_section() -> PrimeAgentCompatibilitySection:
    return PrimeAgentCompatibilitySection()
