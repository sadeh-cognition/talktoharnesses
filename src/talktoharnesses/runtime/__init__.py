"""Lifecycle-only runtime management over remote split services."""

from __future__ import annotations

from talktoharnesses.runtime.events import (
    ProcessEvent,
    ProcessExitedEvent,
    ProcessForcedTerminationEvent,
    ProcessSilenceWarningEvent,
    ProcessStartedEvent,
    ProcessStderrTruncatedEvent,
)
from talktoharnesses.runtime.manager import ManagedRuntime, RuntimeManager
from talktoharnesses.runtime.policy import RuntimePolicy

__all__ = [
    "ManagedRuntime",
    "ProcessEvent",
    "ProcessExitedEvent",
    "ProcessForcedTerminationEvent",
    "ProcessSilenceWarningEvent",
    "ProcessStartedEvent",
    "ProcessStderrTruncatedEvent",
    "RuntimeManager",
    "RuntimePolicy",
]
