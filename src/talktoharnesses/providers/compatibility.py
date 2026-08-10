"""Provider-neutral compatibility envelope and SUPPORTED_HARNESSES rendering."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from importlib.metadata import version as package_version
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError

_COMPAT = ConfigDict(extra="forbid", frozen=True)

PlatformName = Literal["linux", "darwin", "win32"]
MatrixMode = Literal["create", "resume"]
ValidationMode = Literal["development", "stable"]
_KNOWN_PLATFORMS: frozenset[str] = frozenset({"linux", "darwin", "win32"})


class CompatibilityMatrixEntry(BaseModel):
    """Exact create/resume support claim for one release on one platform."""

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

    @property
    def create_matrix(self) -> Sequence[CompatibilityMatrixEntry]: ...

    @property
    def resume_matrix(self) -> Sequence[CompatibilityMatrixEntry]: ...

    def render_release_rows(self) -> list[str]:
        """Return Markdown table rows (without header) for known releases."""
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
    create_matrix: Sequence[CompatibilityMatrixEntry],
    resume_matrix: Sequence[CompatibilityMatrixEntry],
    harness_label: str,
) -> None:
    """Reject unknown, duplicate, platform-mismatched, or resume-incapable matrix rows."""
    by_id = _release_map(releases)
    for mode, matrix in (("create", create_matrix), ("resume", resume_matrix)):
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
            if mode == "resume" and not release.capabilities.supports_resume:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "resume matrix entry for release without resume capability",
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
        lines.extend(
            _render_matrix_section(
                "### Published create matrix",
                section.create_matrix,
                empty_message=(
                    "_No published create combinations yet. "
                    "Create rows are added only after the opt-in create suite passes._"
                ),
            )
        )
        lines.extend(
            _render_matrix_section(
                "### Published resume matrix",
                section.resume_matrix,
                empty_message=(
                    "_No published resume combinations yet. "
                    "Resume rows are added only after the opt-in resume suite passes._"
                ),
            )
        )
        extra = section.render_extra_notes()
        if extra:
            lines.extend(extra)
            if lines[-1] != "":
                lines.append("")
    return "\n".join(lines)


def _load_provider_documents() -> list[
    tuple[
        str,
        str,
        Sequence[ReleaseLike],
        Sequence[CompatibilityMatrixEntry],
        Sequence[CompatibilityMatrixEntry],
    ]
]:
    """Load every packaged provider document for validation."""
    from talktoharnesses.providers.claude.compatibility import load_claude_compatibility
    from talktoharnesses.providers.codex.compatibility import load_codex_compatibility
    from talktoharnesses.providers.cursor.compatibility import load_cursor_compatibility
    from talktoharnesses.providers.grok.compatibility import load_grok_compatibility
    from talktoharnesses.providers.opencode.compatibility import load_opencode_compatibility
    from talktoharnesses.providers.prime_agent.compatibility import load_prime_agent_compatibility

    docs: list[
        tuple[
            str,
            str,
            Sequence[ReleaseLike],
            Sequence[CompatibilityMatrixEntry],
            Sequence[CompatibilityMatrixEntry],
        ]
    ] = []
    for label, loader in (
        ("grok", load_grok_compatibility),
        ("cursor", load_cursor_compatibility),
        ("codex", load_codex_compatibility),
        ("claude", load_claude_compatibility),
        ("opencode", load_opencode_compatibility),
        ("prime_agent", load_prime_agent_compatibility),
    ):
        doc = loader()
        docs.append(
            (
                label,
                doc.adapter_version,
                doc.releases,
                doc.create_matrix,
                doc.resume_matrix,
            )
        )
    return docs


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
    for label, adapter_version, releases, create_matrix, resume_matrix in docs:
        validate_matrices(
            releases=releases,
            create_matrix=create_matrix,
            resume_matrix=resume_matrix,
            harness_label=label,
        )
        if mode == "stable":
            if is_development_version(installed) or is_development_version(adapter_version):
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "stable validation rejects development versions",
                    details={
                        "harness": label,
                        "package_version": installed,
                        "adapter_version": adapter_version,
                    },
                )
            if adapter_version != installed:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "adapter version must equal installed package version",
                    details={
                        "harness": label,
                        "adapter_version": adapter_version,
                        "package_version": installed,
                    },
                )
            if not create_matrix or not resume_matrix:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "stable release requires non-empty create and resume matrices",
                    details={
                        "harness": label,
                        "create_count": len(create_matrix),
                        "resume_count": len(resume_matrix),
                    },
                )
            if not releases:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "stable release requires pinned release records",
                    details={"harness": label},
                )


class EmptyExtraNotes(BaseModel):
    """Mixin helper for sections with no trailing notes."""

    model_config = _COMPAT

    def render_extra_notes(self) -> list[str]:
        return []


def yes_no(value: bool) -> str:
    return "yes" if value else "no"


class SharedMatrices(BaseModel):
    """Shared create/resume matrix containers."""

    model_config = _COMPAT

    create_matrix: list[CompatibilityMatrixEntry] = Field(
        default_factory=list[CompatibilityMatrixEntry]
    )
    resume_matrix: list[CompatibilityMatrixEntry] = Field(
        default_factory=list[CompatibilityMatrixEntry]
    )
