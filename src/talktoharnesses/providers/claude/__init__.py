"""Claude Code harness adapter (claude-agent-sdk)."""

from __future__ import annotations

from talktoharnesses.providers.claude.adapter import ClaudeAdapter
from talktoharnesses.providers.claude.compatibility import (
    ClaudeCompatibilityDoc,
    ClaudeReleaseRecord,
    load_claude_compatibility,
    match_release,
)

__all__ = [
    "ClaudeAdapter",
    "ClaudeCompatibilityDoc",
    "ClaudeReleaseRecord",
    "load_claude_compatibility",
    "match_release",
]
