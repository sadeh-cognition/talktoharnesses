"""Prime Agent compatibility floor and probed-identity matching."""

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
from talktoharnesses.providers.prime_agent.argv import THINKING_LEVELS

_COMPAT = ConfigDict(extra="forbid", frozen=True)


class PrimeAgentFloor(CompatibilityFloor):
    notes: str | None = None


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


class PrimeAgentCompatibilityDoc(BaseModel):
    model_config = _COMPAT

    adapter_version: str
    floor: PrimeAgentFloor
    latest_verified: LatestVerified | None = None


@lru_cache(maxsize=1)
def load_prime_agent_compatibility() -> PrimeAgentCompatibilityDoc:
    root = resources.files("talktoharnesses.data.compatibility")
    data = (root / "prime_agent.json").read_text(encoding="utf-8")
    doc = PrimeAgentCompatibilityDoc.model_validate(json.loads(data))
    validate_floor_document(doc, harness_label="prime_agent", compare=compare_dotted)
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
    floor = doc.floor
    assert_supported_platform(selected_platform, floor.platforms, harness_label="prime_agent")
    reject_below_floor(
        probed=version,
        floor=floor.version,
        compare=compare_dotted,
        harness_label="prime_agent",
        details={"cli_version": version},
    )
    return PrimeAgentReleaseRecord(
        id=f"prime-agent-{version}",
        cli_version=version,
        platforms=list(floor.platforms),
        capabilities=floor.capabilities,
        notes=floor.notes,
    )


def enforce_published_operation(
    release: PrimeAgentReleaseRecord,
    *,
    mode: MatrixMode,
    platform: str | None = None,
    enforce_published: bool = True,
) -> None:
    enforce_operation(
        release.capabilities,
        mode=mode,
        platforms=release.platforms,
        harness_label="prime_agent",
        platform=platform,
        enforce=enforce_published,
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
        return ["- Transport: JSONL RPC"]

    def render_extra_notes(self) -> list[str]:
        notes = self._doc.floor.notes
        if not notes:
            return []
        return ["### Notes", "", f"- {notes}"]


def prime_agent_compatibility_section() -> PrimeAgentCompatibilitySection:
    return PrimeAgentCompatibilitySection()
