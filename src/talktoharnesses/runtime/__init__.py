"""Django-free process supervision and lifecycle-only runtime management."""

from __future__ import annotations

from talktoharnesses.runtime.events import (
    ProcessEvent,
    ProcessExitedEvent,
    ProcessForcedTerminationEvent,
    ProcessSilenceWarningEvent,
    ProcessStartedEvent,
    ProcessStderrTruncatedEvent,
)
from talktoharnesses.runtime.handle import STDERR_RETENTION_BYTES, ProcessHandle
from talktoharnesses.runtime.manager import ManagedRuntime, RuntimeManager
from talktoharnesses.runtime.policy import RuntimePolicy
from talktoharnesses.runtime.spec import ProcessSpec
from talktoharnesses.runtime.supervisor import ProcessSupervisor

__all__ = [
    "STDERR_RETENTION_BYTES",
    "ManagedRuntime",
    "ProcessEvent",
    "ProcessExitedEvent",
    "ProcessForcedTerminationEvent",
    "ProcessHandle",
    "ProcessSilenceWarningEvent",
    "ProcessSpec",
    "ProcessStartedEvent",
    "ProcessStderrTruncatedEvent",
    "ProcessSupervisor",
    "RuntimeManager",
    "RuntimePolicy",
]
