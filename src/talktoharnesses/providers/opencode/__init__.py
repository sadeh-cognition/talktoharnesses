"""OpenCode harness adapter (serve HTTP/SSE)."""

from __future__ import annotations

from talktoharnesses.providers.opencode.adapter import OpenCodeAdapter
from talktoharnesses.providers.opencode.compatibility import (
    OpenCodeCompatibilityDoc,
    OpenCodeReleaseRecord,
    load_opencode_compatibility,
    match_release,
)

__all__ = [
    "OpenCodeAdapter",
    "OpenCodeCompatibilityDoc",
    "OpenCodeReleaseRecord",
    "load_opencode_compatibility",
    "match_release",
]
