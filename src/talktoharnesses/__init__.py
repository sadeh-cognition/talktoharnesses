"""talktoharnesses — unified async interface for coding-agent harnesses."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from talktoharnesses.adapter import Harness
from talktoharnesses.errors import (
    ApprovalError,
    MissingDependencyError,
    ProcessError,
    ProtocolError,
    SessionError,
    TalkToHarnessesError,
    TimeoutError,
    TransportError,
    UnknownHarnessError,
)
from talktoharnesses.events import (
    ContentDelta,
    ItemCompleted,
    ItemStarted,
    ItemUpdated,
    RequestOpened,
    RequestResolved,
    RuntimeErrorEvent,
    RuntimeEvent,
    RuntimeWarning,
    SessionConfigured,
    SessionExited,
    SessionStarted,
    ThreadStarted,
    ThreadTokenUsageUpdated,
    TurnAborted,
    TurnCompleted,
    TurnDiffUpdated,
    TurnPlanUpdated,
    TurnStarted,
    UserInputRequested,
    UserInputResolved,
    parse_runtime_event,
    runtime_event_to_dict,
)
from talktoharnesses.registry import (
    KNOWN_HARNESS_NAMES,
    create_harness,
    ensure_drivers_loaded,
    register,
    registered_names,
)
from talktoharnesses.types import (
    ApprovalDecision,
    Capabilities,
    SendTurnInput,
    Session,
    SessionStartInput,
)

__version__ = "0.1.0"

__all__ = [
    "ApprovalDecision",
    "ApprovalError",
    "Capabilities",
    "ContentDelta",
    "Harness",
    "ItemCompleted",
    "ItemStarted",
    "ItemUpdated",
    "KNOWN_HARNESS_NAMES",
    "MissingDependencyError",
    "ProcessError",
    "ProtocolError",
    "RequestOpened",
    "RequestResolved",
    "RuntimeErrorEvent",
    "RuntimeEvent",
    "RuntimeWarning",
    "SendTurnInput",
    "Session",
    "SessionConfigured",
    "SessionError",
    "SessionExited",
    "SessionStartInput",
    "SessionStarted",
    "TalkToHarnessesError",
    "ThreadStarted",
    "ThreadTokenUsageUpdated",
    "TimeoutError",
    "TransportError",
    "TurnAborted",
    "TurnCompleted",
    "TurnDiffUpdated",
    "TurnPlanUpdated",
    "TurnStarted",
    "UnknownHarnessError",
    "UserInputRequested",
    "UserInputResolved",
    "__version__",
    "create_harness",
    "harness",
    "parse_runtime_event",
    "register",
    "registered_names",
    "runtime_event_to_dict",
]


@asynccontextmanager
async def harness(
    name: str,
    *,
    cwd: str | Path = ".",
    **config: Any,
) -> AsyncIterator[Harness]:
    """Open a harness by name as an async context manager.

    Example::

        async with harness("codex", cwd=".") as h:
            await h.start_session()
            async for ev in h.send_turn("fix the failing tests"):
                ...
    """
    ensure_drivers_loaded()
    path = Path(cwd).resolve()
    h = create_harness(name, cwd=path, **config)
    try:
        yield h
    finally:
        await h.aclose()
