"""Shared ACP runtime for Cursor and Grok drivers."""

from talktoharnesses.acp.normalize import normalize_session_update
from talktoharnesses.acp.runtime import AcpRuntime, AcpSpawnInput

__all__ = [
    "AcpRuntime",
    "AcpSpawnInput",
    "normalize_session_update",
]
