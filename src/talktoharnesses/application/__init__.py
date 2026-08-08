"""Application-layer protocols (persistence and publication)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from talktoharnesses.application.persistence import Persistence
from talktoharnesses.application.publisher import (
    CommittedEventBroker,
    CommittedEventPublisher,
    ConversationWakeup,
)
from talktoharnesses.application.redaction import StreamingTextRedactor

# TalkToHarnessesService / CommandProcessor pull runtime; lazy-export them so
# ``import talktoharnesses.application`` stays Django-free and cycle-free.

if TYPE_CHECKING:
    from talktoharnesses.application.service import TalkToHarnessesService as TalkToHarnessesService

__all__ = [
    "CommittedEventBroker",
    "CommittedEventPublisher",
    "ConversationWakeup",
    "Persistence",
    "StreamingTextRedactor",
    "TalkToHarnessesService",
]


def __getattr__(name: str) -> object:
    if name == "TalkToHarnessesService":
        from talktoharnesses.application.service import TalkToHarnessesService

        return TalkToHarnessesService
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
