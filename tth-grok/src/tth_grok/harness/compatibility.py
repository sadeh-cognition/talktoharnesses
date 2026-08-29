"""Grok compatibility floor and probed-identity matching."""

from __future__ import annotations

import json
import re
import sys
from functools import lru_cache
from importlib import resources

from pydantic import BaseModel, ConfigDict, Field
from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError
from tth_types.harness import HarnessCapabilities, HarnessEffortInfo

from tth_grok.shared.compatibility import (
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

GrokReleaseCapabilities = ReleaseCapabilities

_VERSION_RE = re.compile(
    r"^grok\s+(?P<version>\d+\.\d+\.\d+)\s+\((?P<build>[0-9a-fA-F]+)\)"
    r"(?:\s+\[(?P<channel>[^\]]+)\])?$",
)


class GrokFloor(CompatibilityFloor):
    agent_name: str
    acp_protocol_version: int = 1
    required_agent_methods: list[str] = Field(default_factory=list)
    allowlisted_extensions: list[str] = Field(default_factory=list)
    effort_levels: list[str] = Field(default_factory=list)
    notes: str | None = None


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
                HarnessEffortInfo(id=level, label=level.title()) for level in self.effort_levels
            ),
        )


class GrokCompatibilityDoc(BaseModel):
    model_config = _COMPAT

    adapter_version: str
    floor: GrokFloor
    latest_verified: LatestVerified | None = None


@lru_cache(maxsize=1)
def load_grok_compatibility() -> GrokCompatibilityDoc:
    """Load the packaged Grok compatibility document."""
    root = resources.files("tth_grok.data.compatibility")
    data = (root / "grok.json").read_text(encoding="utf-8")
    doc = GrokCompatibilityDoc.model_validate(json.loads(data))
    validate_floor_document(doc, harness_label="grok", compare=compare_dotted)
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
    """Match version stdout against the Grok floor; accept identities at or above it."""
    version, build, channel = parse_version_stdout(version_stdout)
    plat = platform or sys.platform
    doc = load_grok_compatibility()
    floor = doc.floor
    assert_supported_platform(plat, floor.platforms, harness_label="grok")
    reject_below_floor(
        probed=version,
        floor=floor.version,
        compare=compare_dotted,
        harness_label="grok",
        details={
            "provider": "Grok",
            "installed_version": f"{version} ({build}) [{channel}]",
            "cli_version": version,
            "cli_build": build,
            "cli_channel": channel,
        },
    )
    return GrokReleaseRecord(
        id=f"grok-{version}-{build}",
        cli_version=version,
        cli_build=build,
        cli_channel=channel,
        version_stdout_prefix=f"grok {version} ({build})",
        agent_name=floor.agent_name,
        acp_protocol_version=floor.acp_protocol_version,
        platforms=list(floor.platforms),
        capabilities=floor.capabilities,
        required_agent_methods=list(floor.required_agent_methods),
        allowlisted_extensions=list(floor.allowlisted_extensions),
        effort_levels=list(floor.effort_levels),
        notes=floor.notes,
    )


def enforce_published_operation(
    release: GrokReleaseRecord,
    *,
    mode: MatrixMode,
    platform: str | None = None,
    enforce_published: bool = True,
) -> None:
    """Validate the probed identity against the floor platform and capability flags."""
    enforce_operation(
        release.capabilities,
        mode=mode,
        platforms=release.platforms,
        harness_label="grok",
        platform=platform,
        enforce=enforce_published,
    )
