"""Provider-neutral compatibility floor, operation gating, and SUPPORTED_HARNESSES rendering."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from importlib.metadata import version as package_version
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import HarnessCapabilities, VersionAdvisory

_COMPAT = ConfigDict(extra="forbid", frozen=True)

PlatformName = Literal["linux", "darwin", "win32"]
MatrixMode = Literal[
    "create",
    "resume",
    "steer",
    "interrupt",
    "multi_interaction",
    "nested_activity",
]
ValidationMode = Literal["development", "stable"]
VersionCompare = Callable[[str, str], int | None]
_KNOWN_PLATFORMS: frozenset[str] = frozenset({"linux", "darwin", "win32"})
MATRIX_MODES: tuple[MatrixMode, ...] = (
    "create",
    "resume",
    "steer",
    "interrupt",
    "multi_interaction",
    "nested_activity",
)
CAPABILITY_FLAG_FOR_MODE: dict[MatrixMode, str] = {
    "resume": "supports_resume",
    "steer": "supports_steer",
    "interrupt": "supports_interrupt",
    "multi_interaction": "supports_multi_interaction",
    "nested_activity": "supports_nested_activity",
}
CAPABILITY_TABLE_HEADER = "Resume | Interrupt | Steer | Multi-interaction | Nested"
CAPABILITY_TABLE_DIVIDER = "--- | --- | --- | --- | ---"


class ReleaseCapabilities(BaseModel):
    """Adapter-owned capability flags copied onto probed identities."""

    model_config = _COMPAT

    supports_resume: bool = False
    supports_interrupt: bool = True
    supports_steer: bool = False
    supports_multi_interaction: bool = False
    supports_nested_activity: bool = False


class LatestVerified(BaseModel):
    """Last live-proven identity. Advisory only; not an allowlist."""

    model_config = _COMPAT

    version: str
    identity: str | None = None
    platform: PlatformName | None = None


class CompatibilityFloor(BaseModel):
    """Minimum identity the adapter will drive on published platforms."""

    model_config = _COMPAT

    version: str
    platforms: list[PlatformName] = Field(default_factory=list[PlatformName])
    capabilities: ReleaseCapabilities = Field(default_factory=ReleaseCapabilities)


class FloorDocument(Protocol):
    """Packaged JSON document shape used by validation."""

    @property
    def adapter_version(self) -> str: ...

    @property
    def floor(self) -> CompatibilityFloor: ...

    @property
    def latest_verified(self) -> LatestVerified | None: ...


class CompatibilitySection(Protocol):
    """One harness contribution to the generated support document."""

    @property
    def kind(self) -> HarnessKind: ...

    @property
    def adapter_version(self) -> str: ...

    @property
    def floor_label(self) -> str: ...

    @property
    def platforms(self) -> Sequence[str]: ...

    @property
    def capabilities(self) -> ReleaseCapabilities: ...

    @property
    def latest_verified(self) -> LatestVerified | None: ...

    def render_extra_floor_lines(self) -> list[str]:
        """Optional extra bullet lines under the floor summary."""
        ...

    def render_extra_notes(self) -> list[str]:
        """Optional trailing Markdown lines for this harness section."""
        ...


_SectionLoader = Callable[[], CompatibilitySection]


def is_development_version(version: str) -> bool:
    """Return True for PEP 440 development versions (``.devN``)."""
    normalized = version.split("+", 1)[0].lower()
    return ".dev" in normalized


def installed_package_version() -> str:
    """Return the installed talktoharnesses distribution version."""
    return package_version("talktoharnesses")


def compare_dotted(left: str, right: str) -> int | None:
    """Compare dotted numeric versions (``1.0.5``, ``0.144.4``)."""
    parsed_left = _dotted_parts(left)
    parsed_right = _dotted_parts(right)
    if parsed_left is None or parsed_right is None:
        return None
    length = max(len(parsed_left), len(parsed_right))
    padded_left = parsed_left + (0,) * (length - len(parsed_left))
    padded_right = parsed_right + (0,) * (length - len(parsed_right))
    if padded_left < padded_right:
        return -1
    if padded_left > padded_right:
        return 1
    return 0


def compare_cursor_date(left: str, right: str) -> int | None:
    """Compare Cursor ``YYYY.MM.DD`` prefixes; ignore the trailing build hash."""
    parsed_left = _cursor_date_parts(left)
    parsed_right = _cursor_date_parts(right)
    if parsed_left is None or parsed_right is None:
        return None
    if parsed_left < parsed_right:
        return -1
    if parsed_left > parsed_right:
        return 1
    return 0


def version_advisory(
    *,
    probed: str,
    floor: str,
    latest_verified: str | None,
    compare: VersionCompare,
) -> VersionAdvisory:
    """Compare a probed identity to the floor and last live proof. Never raises."""
    if compare(probed, floor) is None:
        return VersionAdvisory(
            status="unknown",
            probed_version=probed,
            floor_version=floor,
            latest_verified=latest_verified,
        )
    if latest_verified is None or compare(probed, latest_verified) is None:
        return VersionAdvisory(
            status="unknown",
            probed_version=probed,
            floor_version=floor,
            latest_verified=latest_verified,
        )
    versus_latest = compare(probed, latest_verified)
    if versus_latest == 0:
        status: Literal["verified", "behind_verified", "ahead_of_verified", "unknown"] = "verified"
    elif versus_latest is not None and versus_latest < 0:
        status = "behind_verified"
    else:
        status = "ahead_of_verified"
    return VersionAdvisory(
        status=status,
        probed_version=probed,
        floor_version=floor,
        latest_verified=latest_verified,
    )


def comparable_probe_version(capabilities: HarnessCapabilities) -> str:
    """Extract the identity used for floor/advisory comparison."""
    raw = capabilities.version
    if capabilities.kind is HarnessKind.GROK:
        return raw.split()[0] if raw.split() else raw
    if capabilities.kind is HarnessKind.CURSOR:
        return raw.split("-", 1)[0]
    if capabilities.kind is HarnessKind.CLAUDE and "+cli-" in raw:
        return raw.split("+cli-", 1)[1]
    return raw


def advisory_for_capabilities(capabilities: HarnessCapabilities) -> VersionAdvisory:
    """Build an advisory from a probed capabilities snapshot and packaged floor."""
    floor_version, latest, compare = _floor_compare_for_kind(capabilities.kind)
    return version_advisory(
        probed=comparable_probe_version(capabilities),
        floor=floor_version,
        latest_verified=latest,
        compare=compare,
    )


def assert_supported_platform(
    platform: str,
    platforms: Sequence[str],
    *,
    harness_label: str,
) -> None:
    """Reject unknown or unpublished platforms."""
    if platform not in _KNOWN_PLATFORMS:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            f"unsupported platform for {harness_label}",
            details={"harness": harness_label, "platform": platform},
        )
    if platform not in platforms:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            f"{harness_label} release not supported on this platform",
            details={
                "platform": platform,
                "supported_platforms": list(platforms),
            },
        )


def reject_below_floor(
    *,
    probed: str,
    floor: str,
    compare: VersionCompare,
    harness_label: str,
    details: dict[str, object],
) -> None:
    """Fail when the probed identity is older than the packaged floor."""
    versus = compare(probed, floor)
    if versus is None:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            f"malformed {harness_label} version for floor comparison",
            details=details,
        )
    if versus < 0:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            f"{harness_label} release is below the compatibility floor",
            details={**details, "floor_version": floor, "probed_version": probed},
        )


def enforce_operation(
    capabilities: ReleaseCapabilities,
    *,
    mode: MatrixMode,
    platforms: Sequence[str],
    harness_label: str,
    platform: str | None = None,
    enforce: bool = True,
) -> None:
    """Gate create/resume/steer/interrupt by floor platform and capability flags."""
    if not enforce:
        return
    assert_supported_platform(
        platform or sys.platform,
        platforms,
        harness_label=harness_label,
    )
    if mode == "create":
        return
    flag = CAPABILITY_FLAG_FOR_MODE.get(mode)
    if flag is None or bool(getattr(capabilities, flag)):
        return
    raise DomainError(
        ErrorCode.PROVIDER_INCOMPATIBLE,
        f"{harness_label} does not advertise {mode}",
        details={"harness": harness_label, "mode": mode},
    )


def validate_floor_document(
    doc: FloorDocument,
    *,
    harness_label: str,
    compare: VersionCompare,
) -> None:
    """Reject malformed floors or latest_verified entries below the floor."""
    if not doc.floor.version.strip():
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "compatibility floor is missing a version",
            details={"harness": harness_label},
        )
    if compare(doc.floor.version, doc.floor.version) is None:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "compatibility floor version is malformed",
            details={"harness": harness_label, "floor_version": doc.floor.version},
        )
    if not doc.floor.platforms:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "compatibility floor requires at least one platform",
            details={"harness": harness_label},
        )
    for platform in doc.floor.platforms:
        if platform not in _KNOWN_PLATFORMS:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "unsupported platform in compatibility floor",
                details={"harness": harness_label, "platform": platform},
            )
    latest = doc.latest_verified
    if latest is None:
        return
    versus = compare(latest.version, doc.floor.version)
    if versus is not None and versus < 0:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "latest_verified is below the compatibility floor",
            details={
                "harness": harness_label,
                "floor_version": doc.floor.version,
                "latest_verified": latest.version,
            },
        )
    if latest.platform is not None and latest.platform not in doc.floor.platforms:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "latest_verified platform is absent from the floor",
            details={
                "harness": harness_label,
                "platform": latest.platform,
                "supported_platforms": list(doc.floor.platforms),
            },
        )


def yes_no(value: bool) -> str:
    return "yes" if value else "no"


def capability_cells(caps: ReleaseCapabilities) -> str:
    """Markdown cells for the shared capability columns."""
    return " | ".join(
        (
            yes_no(caps.supports_resume),
            yes_no(caps.supports_interrupt),
            yes_no(caps.supports_steer),
            yes_no(caps.supports_multi_interaction),
            yes_no(caps.supports_nested_activity),
        )
    )


def _default_loaders() -> list[_SectionLoader]:
    """Load installed provider documents in fixed HarnessKind order."""
    loaders: list[_SectionLoader] = []

    def _grok() -> CompatibilitySection:
        from talktoharnesses.providers.grok.compatibility import grok_compatibility_section

        return grok_compatibility_section()

    loaders.append(_grok)

    try:
        from talktoharnesses.providers.cursor.compatibility import cursor_compatibility_section
    except ImportError:
        pass
    else:
        loaders.append(cursor_compatibility_section)

    try:
        from talktoharnesses.providers.codex.compatibility import codex_compatibility_section
    except ImportError:
        pass
    else:
        loaders.append(codex_compatibility_section)

    try:
        from talktoharnesses.providers.claude.compatibility import claude_compatibility_section
    except ImportError:
        pass
    else:
        loaders.append(claude_compatibility_section)

    try:
        from talktoharnesses.providers.opencode.compatibility import opencode_compatibility_section
    except ImportError:
        pass
    else:
        loaders.append(opencode_compatibility_section)

    try:
        from talktoharnesses.providers.prime_agent.compatibility import (
            prime_agent_compatibility_section,
        )
    except ImportError:
        pass
    else:
        loaders.append(prime_agent_compatibility_section)

    return loaders


def load_compatibility_sections(
    loaders: Sequence[_SectionLoader] | None = None,
) -> list[CompatibilitySection]:
    """Load sections; order follows HarnessKind enum when using defaults."""
    if loaders is not None:
        return [loader() for loader in loaders]
    sections = [loader() for loader in _default_loaders()]
    order = {kind: index for index, kind in enumerate(HarnessKind)}
    return sorted(sections, key=lambda section: order.get(section.kind, 999))


def render_supported_harnesses_markdown(
    sections: Sequence[CompatibilitySection] | None = None,
) -> str:
    """Generate SUPPORTED_HARNESSES.md from installed provider documents."""
    resolved = list(sections) if sections is not None else load_compatibility_sections()
    lines = [
        "# Supported Harnesses",
        "",
        "This document is generated from packaged compatibility data.",
        "Do not edit provider tables by hand; regenerate via",
        "`python -m talktoharnesses.providers.render_supported`.",
        "",
        "Each harness publishes a **floor** (minimum identity and platforms) and",
        "adapter-owned capability flags. Models, modes, and efforts are discovered",
        "at probe from the installed CLI. Newer identities above the floor are",
        "accepted; `latest_verified` is advisory only.",
        "",
    ]
    if not resolved:
        lines.append("_No harness compatibility documents installed._")
        lines.append("")
        return "\n".join(lines)

    for section in resolved:
        title = section.kind.value.replace("_", " ").title()
        if section.kind is HarnessKind.OPENCODE:
            title = "OpenCode"
        elif section.kind is HarnessKind.CLAUDE:
            title = "Claude Code"
        elif section.kind is HarnessKind.PRIME_AGENT:
            title = "Prime Agent"
        platforms = ", ".join(section.platforms) if section.platforms else "—"
        lines.extend(
            (
                f"## {title}",
                "",
                f"- Adapter version: `{section.adapter_version}`",
                f"- Floor: {section.floor_label} on {platforms}",
            )
        )
        latest = section.latest_verified
        if latest is None:
            lines.append("- Latest verified: _none_")
        else:
            identity = latest.identity or latest.version
            latest_platform = latest.platform or "—"
            lines.append(f"- Latest verified: `{identity}` on {latest_platform}")
        lines.append(
            "- Models, modes, and efforts are discovered at probe from the installed CLI."
        )
        extra_floor = section.render_extra_floor_lines()
        if extra_floor:
            lines.extend(extra_floor)
        lines.extend(
            (
                "",
                "### Adapter capabilities",
                "",
                f"| {CAPABILITY_TABLE_HEADER} |",
                f"| {CAPABILITY_TABLE_DIVIDER} |",
                f"| {capability_cells(section.capabilities)} |",
                "",
            )
        )
        extra = section.render_extra_notes()
        if extra:
            lines.extend(extra)
            if lines[-1] != "":
                lines.append("")
    return "\n".join(lines)


def _load_provider_documents() -> list[tuple[str, FloorDocument, VersionCompare]]:
    """Load every packaged provider document for validation."""
    from talktoharnesses.providers.claude.compatibility import load_claude_compatibility
    from talktoharnesses.providers.codex.compatibility import load_codex_compatibility
    from talktoharnesses.providers.cursor.compatibility import load_cursor_compatibility
    from talktoharnesses.providers.grok.compatibility import load_grok_compatibility
    from talktoharnesses.providers.opencode.compatibility import load_opencode_compatibility
    from talktoharnesses.providers.prime_agent.compatibility import load_prime_agent_compatibility

    return [
        ("grok", load_grok_compatibility(), compare_dotted),
        ("cursor", load_cursor_compatibility(), compare_cursor_date),
        ("codex", load_codex_compatibility(), compare_dotted),
        ("claude", load_claude_compatibility(), compare_dotted),
        ("opencode", load_opencode_compatibility(), compare_dotted),
        ("prime_agent", load_prime_agent_compatibility(), compare_dotted),
    ]


def validate_compatibility_documents(*, mode: ValidationMode = "development") -> None:
    """Validate packaged compatibility documents for development or stable release."""
    installed = installed_package_version()
    docs = _load_provider_documents()
    if len(docs) != len(HarnessKind):
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "expected one compatibility document per harness kind",
            details={"count": len(docs)},
        )
    for label, doc, compare in docs:
        validate_floor_document(doc, harness_label=label, compare=compare)
        if mode != "stable":
            continue
        if is_development_version(installed) or is_development_version(doc.adapter_version):
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "stable validation rejects development versions",
                details={
                    "harness": label,
                    "package_version": installed,
                    "adapter_version": doc.adapter_version,
                },
            )
        if doc.adapter_version != installed:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "adapter version must equal installed package version",
                details={
                    "harness": label,
                    "adapter_version": doc.adapter_version,
                    "package_version": installed,
                },
            )


def _dotted_parts(version: str) -> tuple[int, ...] | None:
    if not version or any(char.isspace() for char in version):
        return None
    parts = version.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _cursor_date_parts(value: str) -> tuple[int, int, int] | None:
    date = value.split("-", 1)[0]
    parts = _dotted_parts(date)
    if parts is None or len(parts) != 3:
        return None
    year, month, day = parts
    if year < 2000 or not (1 <= month <= 12) or not (1 <= day <= 31):
        return None
    return year, month, day


def _floor_compare_for_kind(
    kind: HarnessKind,
) -> tuple[str, str | None, VersionCompare]:
    from talktoharnesses.providers.claude.compatibility import load_claude_compatibility
    from talktoharnesses.providers.codex.compatibility import load_codex_compatibility
    from talktoharnesses.providers.cursor.compatibility import load_cursor_compatibility
    from talktoharnesses.providers.grok.compatibility import load_grok_compatibility
    from talktoharnesses.providers.opencode.compatibility import load_opencode_compatibility
    from talktoharnesses.providers.prime_agent.compatibility import load_prime_agent_compatibility

    loaders: dict[HarnessKind, tuple[Callable[[], FloorDocument], VersionCompare]] = {
        HarnessKind.GROK: (load_grok_compatibility, compare_dotted),
        HarnessKind.CURSOR: (load_cursor_compatibility, compare_cursor_date),
        HarnessKind.CODEX: (load_codex_compatibility, compare_dotted),
        HarnessKind.CLAUDE: (load_claude_compatibility, compare_dotted),
        HarnessKind.OPENCODE: (load_opencode_compatibility, compare_dotted),
        HarnessKind.PRIME_AGENT: (load_prime_agent_compatibility, compare_dotted),
    }
    loader, compare = loaders[kind]
    doc = loader()
    latest = doc.latest_verified.version if doc.latest_verified is not None else None
    return doc.floor.version, latest, compare
