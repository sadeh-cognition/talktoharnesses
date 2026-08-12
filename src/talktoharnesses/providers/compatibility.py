"""Provider-neutral compatibility envelope and SUPPORTED_HARNESSES rendering."""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from importlib.metadata import version as package_version
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError

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
_MATRIX_HEADINGS: dict[MatrixMode, tuple[str, str]] = {
    "create": (
        "### Published create matrix",
        "Create rows are added only after the opt-in create suite passes.",
    ),
    "resume": (
        "### Published resume matrix",
        "Resume rows are added only after the opt-in resume suite passes.",
    ),
    "steer": (
        "### Published steer matrix",
        "Steer rows are added only after the opt-in steer gate passes.",
    ),
    "interrupt": (
        "### Published interrupt matrix",
        "Interrupt rows are added only after the opt-in interrupt gate passes.",
    ),
    "multi_interaction": (
        "### Published multi-interaction matrix",
        "Multi-interaction rows are added only after the opt-in multi-interaction gate passes.",
    ),
    "nested_activity": (
        "### Published nested-activity matrix",
        "Nested-activity rows are added only after the opt-in nested-activity gate passes.",
    ),
}
CAPABILITY_TABLE_HEADER = "Resume | Interrupt | Steer | Multi-interaction | Nested"
CAPABILITY_TABLE_DIVIDER = "--- | --- | --- | --- | ---"


class CompatibilityMatrixEntry(BaseModel):
    """Exact support claim for one release on one platform."""

    model_config = _COMPAT

    release_id: str
    platform: PlatformName


class ReleaseCapabilities(BaseModel):
    """Shared capability flags for one exact harness release."""

    model_config = _COMPAT

    supports_resume: bool = False
    supports_interrupt: bool = True
    supports_steer: bool = False
    supports_multi_interaction: bool = False
    supports_nested_activity: bool = False


@runtime_checkable
class ReleaseLike(Protocol):
    """Minimal release shape needed for matrix validation."""

    @property
    def id(self) -> str: ...

    @property
    def platforms(self) -> Sequence[str]: ...

    @property
    def capabilities(self) -> ReleaseCapabilities: ...


class CompatibilitySection(Protocol):
    """One harness contribution to the generated support document."""

    @property
    def kind(self) -> HarnessKind: ...

    @property
    def adapter_version(self) -> str: ...

    def matrix(self, mode: MatrixMode) -> Sequence[CompatibilityMatrixEntry]:
        """Return the published matrix for one operation or capability."""
        ...

    def render_release_rows(self) -> list[str]:
        """Return Markdown table rows (without header) for known releases."""
        ...

    def render_extra_notes(self) -> list[str]:
        """Optional trailing Markdown lines for this harness section."""
        ...


@runtime_checkable
class CompatibilityDocument(Protocol):
    """Packaged JSON document shape used by validation."""

    @property
    def adapter_version(self) -> str: ...

    @property
    def releases(self) -> Sequence[ReleaseLike]: ...

    @property
    def create_matrix(self) -> Sequence[CompatibilityMatrixEntry]: ...

    @property
    def resume_matrix(self) -> Sequence[CompatibilityMatrixEntry]: ...

    def as_mapping(self) -> Mapping[MatrixMode, Sequence[CompatibilityMatrixEntry]]: ...


_SectionLoader = Callable[[], CompatibilitySection]


def is_development_version(version: str) -> bool:
    """Return True for PEP 440 development versions (``.devN``)."""
    normalized = version.split("+", 1)[0].lower()
    return ".dev" in normalized


def installed_package_version() -> str:
    """Return the installed talktoharnesses distribution version."""
    return package_version("talktoharnesses")


def _release_map(releases: Sequence[ReleaseLike]) -> dict[str, ReleaseLike]:
    mapping: dict[str, ReleaseLike] = {}
    for release in releases:
        if release.id in mapping:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "duplicate release id in compatibility document",
                details={"release_id": release.id},
            )
        mapping[release.id] = release
    return mapping


def validate_matrices(
    *,
    releases: Sequence[ReleaseLike],
    matrices: Mapping[MatrixMode, Sequence[CompatibilityMatrixEntry]],
    harness_label: str,
) -> None:
    """Reject unknown, duplicate, platform-mismatched, or capability-mismatched rows."""
    by_id = _release_map(releases)
    for mode in MATRIX_MODES:
        matrix = matrices.get(mode, ())
        seen: set[tuple[str, str]] = set()
        for entry in matrix:
            key = (entry.release_id, entry.platform)
            if key in seen:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    f"duplicate {mode} matrix entry",
                    details={
                        "harness": harness_label,
                        "release_id": entry.release_id,
                        "platform": entry.platform,
                    },
                )
            seen.add(key)
            release = by_id.get(entry.release_id)
            if release is None:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    f"unknown release in {mode} matrix",
                    details={
                        "harness": harness_label,
                        "release_id": entry.release_id,
                        "known_releases": sorted(by_id),
                    },
                )
            if entry.platform not in _KNOWN_PLATFORMS:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    f"unsupported platform in {mode} matrix",
                    details={
                        "harness": harness_label,
                        "release_id": entry.release_id,
                        "platform": entry.platform,
                    },
                )
            if entry.platform not in release.platforms:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "matrix platform absent from release record",
                    details={
                        "harness": harness_label,
                        "release_id": entry.release_id,
                        "platform": entry.platform,
                        "supported_platforms": list(release.platforms),
                    },
                )
            flag = CAPABILITY_FLAG_FOR_MODE.get(mode)
            if flag is not None and not bool(getattr(release.capabilities, flag)):
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    f"{mode} matrix entry for release without {flag}",
                    details={
                        "harness": harness_label,
                        "release_id": entry.release_id,
                        "platform": entry.platform,
                    },
                )


def matrix_contains(
    matrix: Sequence[CompatibilityMatrixEntry],
    *,
    release_id: str,
    platform: str,
) -> bool:
    """Return whether an exact release/platform pair is published."""
    return any(entry.release_id == release_id and entry.platform == platform for entry in matrix)


def assert_matrix_membership(
    *,
    release_id: str,
    platform: str,
    matrix: Sequence[CompatibilityMatrixEntry],
    mode: MatrixMode,
    harness_label: str,
    package_version: str | None = None,
    enforce_published: bool = True,
) -> None:
    """Fail when a published matrix is enforced and the release/platform is absent."""
    if not enforce_published:
        return
    version = package_version if package_version is not None else installed_package_version()
    if not matrix:
        if is_development_version(version):
            return
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            f"empty published {mode} matrix is not allowed for a stable release",
            details={"harness": harness_label, "package_version": version},
        )
    if platform not in _KNOWN_PLATFORMS:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            f"unsupported platform for {mode}",
            details={"harness": harness_label, "platform": platform},
        )
    if not matrix_contains(matrix, release_id=release_id, platform=platform):
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            f"{harness_label} release not in published {mode} matrix for this platform",
            details={
                "release_id": release_id,
                "platform": platform,
                "matrix": [
                    {"release_id": entry.release_id, "platform": entry.platform} for entry in matrix
                ],
            },
        )


def sorted_matrix_entries(
    matrix: Sequence[CompatibilityMatrixEntry],
) -> list[CompatibilityMatrixEntry]:
    """Deterministic matrix order: release ID, then platform."""
    return sorted(matrix, key=lambda entry: (entry.release_id, entry.platform))


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


def _render_matrix_section(
    title: str,
    matrix: Sequence[CompatibilityMatrixEntry],
    *,
    empty_message: str,
) -> list[str]:
    lines = [title, ""]
    if not matrix:
        lines.append(empty_message)
        lines.append("")
        return lines
    lines.append("| Release ID | Platform |")
    lines.append("| --- | --- |")
    for entry in sorted_matrix_entries(matrix):
        lines.append(f"| `{entry.release_id}` | `{entry.platform}` |")
    lines.append("")
    return lines


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
        lines.append(f"## {title}")
        lines.append("")
        lines.append(f"- Adapter version: `{section.adapter_version}`")
        lines.append("")
        lines.append("### Known releases (implementation targets)")
        lines.append("")
        rows = section.render_release_rows()
        if not rows:
            lines.append("_No releases recorded._")
            lines.append("")
        else:
            lines.extend(rows)
            if rows and not rows[-1].endswith("\n") and rows[-1] != "":
                lines.append("")
        for mode in MATRIX_MODES:
            heading, empty_reason = _MATRIX_HEADINGS[mode]
            lines.extend(
                _render_matrix_section(
                    heading,
                    section.matrix(mode),
                    empty_message=(
                        f"_No published {mode.replace('_', '-')} combinations yet. {empty_reason}_"
                    ),
                )
            )
        extra = section.render_extra_notes()
        if extra:
            lines.extend(extra)
            if lines[-1] != "":
                lines.append("")
    return "\n".join(lines)


def _load_provider_documents() -> list[tuple[str, CompatibilityDocument]]:
    """Load every packaged provider document for validation."""
    from talktoharnesses.providers.claude.compatibility import load_claude_compatibility
    from talktoharnesses.providers.codex.compatibility import load_codex_compatibility
    from talktoharnesses.providers.cursor.compatibility import load_cursor_compatibility
    from talktoharnesses.providers.grok.compatibility import load_grok_compatibility
    from talktoharnesses.providers.opencode.compatibility import load_opencode_compatibility
    from talktoharnesses.providers.prime_agent.compatibility import load_prime_agent_compatibility

    return [
        ("grok", load_grok_compatibility()),
        ("cursor", load_cursor_compatibility()),
        ("codex", load_codex_compatibility()),
        ("claude", load_claude_compatibility()),
        ("opencode", load_opencode_compatibility()),
        ("prime_agent", load_prime_agent_compatibility()),
    ]


def _require_advertised_matrix_rows(
    *,
    releases: Sequence[ReleaseLike],
    matrices: Mapping[MatrixMode, Sequence[CompatibilityMatrixEntry]],
    harness_label: str,
) -> None:
    """Stable releases must publish a row for every advertised capability."""
    for release in releases:
        for mode, flag in CAPABILITY_FLAG_FOR_MODE.items():
            if not bool(getattr(release.capabilities, flag)):
                continue
            matrix = matrices.get(mode, ())
            if not any(entry.release_id == release.id for entry in matrix):
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    f"advertised {flag} requires a published {mode} matrix row",
                    details={
                        "harness": harness_label,
                        "release_id": release.id,
                    },
                )


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
    for label, doc in docs:
        matrices = doc.as_mapping()
        validate_matrices(
            releases=doc.releases,
            matrices=matrices,
            harness_label=label,
        )
        if mode == "stable":
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
            if not doc.create_matrix or not doc.resume_matrix:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "stable release requires non-empty create and resume matrices",
                    details={
                        "harness": label,
                        "create_count": len(doc.create_matrix),
                        "resume_count": len(doc.resume_matrix),
                    },
                )
            if not doc.releases:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "stable release requires pinned release records",
                    details={"harness": label},
                )
            _require_advertised_matrix_rows(
                releases=doc.releases,
                matrices=matrices,
                harness_label=label,
            )


class EmptyExtraNotes(BaseModel):
    """Mixin helper for sections with no trailing notes."""

    model_config = _COMPAT

    def render_extra_notes(self) -> list[str]:
        return []


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


def enforce_doc_operation(
    doc: SharedMatrices,
    release_id: str,
    *,
    mode: MatrixMode,
    harness_label: str,
    platform: str | None = None,
    enforce_published: bool = True,
) -> None:
    """Validate a probed release against one published matrix on ``doc``."""
    assert_matrix_membership(
        release_id=release_id,
        platform=platform or sys.platform,
        matrix=getattr(doc, f"{mode}_matrix"),
        mode=mode,
        harness_label=harness_label,
        enforce_published=enforce_published,
    )


class SharedMatrices(BaseModel):
    """Shared published-matrix containers for every harness document."""

    model_config = _COMPAT

    create_matrix: list[CompatibilityMatrixEntry] = Field(
        default_factory=list[CompatibilityMatrixEntry]
    )
    resume_matrix: list[CompatibilityMatrixEntry] = Field(
        default_factory=list[CompatibilityMatrixEntry]
    )
    steer_matrix: list[CompatibilityMatrixEntry] = Field(
        default_factory=list[CompatibilityMatrixEntry]
    )
    interrupt_matrix: list[CompatibilityMatrixEntry] = Field(
        default_factory=list[CompatibilityMatrixEntry]
    )
    multi_interaction_matrix: list[CompatibilityMatrixEntry] = Field(
        default_factory=list[CompatibilityMatrixEntry]
    )
    nested_activity_matrix: list[CompatibilityMatrixEntry] = Field(
        default_factory=list[CompatibilityMatrixEntry]
    )

    def as_mapping(self) -> dict[MatrixMode, list[CompatibilityMatrixEntry]]:
        return {mode: list(getattr(self, f"{mode}_matrix")) for mode in MATRIX_MODES}
