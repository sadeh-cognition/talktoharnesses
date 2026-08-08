"""Provider-neutral compatibility envelope and SUPPORTED_HARNESSES rendering."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from talktoharnesses.domain.enums import HarnessKind

_COMPAT = ConfigDict(extra="forbid", frozen=True)


class ReleaseCapabilities(BaseModel):
    """Shared capability flags for one exact harness release."""

    model_config = _COMPAT

    supports_resume: bool = False
    supports_interrupt: bool = True
    supports_steer: bool = False
    supports_multi_interaction: bool = False
    supports_nested_activity: bool = False


class CompatibilitySection(Protocol):
    """One harness contribution to the generated support document."""

    @property
    def kind(self) -> HarnessKind: ...

    @property
    def adapter_version(self) -> str: ...

    @property
    def create_matrix(self) -> Sequence[str]: ...

    @property
    def resume_matrix(self) -> Sequence[str]: ...

    def render_release_rows(self) -> list[str]:
        """Return Markdown table rows (without header) for known releases."""
        ...

    def render_extra_notes(self) -> list[str]:
        """Optional trailing Markdown lines for this harness section."""
        ...


_SectionLoader = Callable[[], CompatibilitySection]


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
            # Provider sections supply a header row as the first element when present.
            lines.extend(rows)
            if rows and not rows[-1].endswith("\n") and rows[-1] != "":
                lines.append("")
        lines.append("### Published create matrix")
        lines.append("")
        if not section.create_matrix:
            lines.append(
                "_No published create combinations yet. "
                "Create rows are added only after the opt-in create suite passes._"
            )
        else:
            for release_id in section.create_matrix:
                lines.append(f"- `{release_id}`")
        lines.append("")
        lines.append("### Published resume matrix")
        lines.append("")
        if not section.resume_matrix:
            lines.append(
                "_No published resume combinations yet. "
                "Resume rows are added only after the opt-in resume suite passes._"
            )
        else:
            for release_id in section.resume_matrix:
                lines.append(f"- `{release_id}`")
        lines.append("")
        extra = section.render_extra_notes()
        if extra:
            lines.extend(extra)
            if lines[-1] != "":
                lines.append("")
    return "\n".join(lines)


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

    create_matrix: list[str] = Field(default_factory=list)
    resume_matrix: list[str] = Field(default_factory=list)
