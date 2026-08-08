"""Grok harness adapter (ACP stdio)."""

from __future__ import annotations

from talktoharnesses.providers.grok.adapter import GrokAdapter
from talktoharnesses.providers.grok.compatibility import (
    GrokCompatibilityDoc,
    GrokReleaseRecord,
    load_grok_compatibility,
    match_release,
    render_supported_harnesses_markdown,
)

__all__ = [
    "GrokAdapter",
    "GrokCompatibilityDoc",
    "GrokReleaseRecord",
    "load_grok_compatibility",
    "match_release",
    "render_supported_harnesses_markdown",
]
