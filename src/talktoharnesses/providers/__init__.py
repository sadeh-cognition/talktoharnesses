"""Provider adapter contract and fixed registry."""

from __future__ import annotations

from talktoharnesses.providers.adapter import (
    HarnessAdapter,
    HarnessInteractionRequest,
    HarnessSession,
    ResumeSessionRequest,
    StartSessionRequest,
    SteerRequest,
    TurnRequest,
)
from talktoharnesses.providers.registry import AdapterRegistry

__all__ = [
    "AdapterRegistry",
    "HarnessAdapter",
    "HarnessInteractionRequest",
    "HarnessSession",
    "ResumeSessionRequest",
    "StartSessionRequest",
    "SteerRequest",
    "TurnRequest",
]
