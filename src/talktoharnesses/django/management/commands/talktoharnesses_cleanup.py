"""Externally scheduled six-month retention pass.

Builds the normal Django composition without starting command workers, runs one
:func:`run_cleanup` pass, and prints its counts. It never reads, changes, or
deletes workspace files (``docs/phase8.md`` Work Package 5).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from django.core.management.base import BaseCommand

from talktoharnesses.application.retention import CleanupCounts, run_cleanup
from talktoharnesses.django.broker import DjangoCommittedEventBroker
from talktoharnesses.django.persistence import DjangoPersistence
from talktoharnesses.providers.default_registry import build_default_adapter_registry
from talktoharnesses.runtime.manager import RuntimeManager


def _utc_clock() -> datetime:
    return datetime.now(UTC)


async def _cleanup() -> CleanupCounts:
    persistence = DjangoPersistence()
    runtime = RuntimeManager(persistence, build_default_adapter_registry(), clock=_utc_clock)
    return await run_cleanup(persistence, runtime, _utc_clock, DjangoCommittedEventBroker())


class Command(BaseCommand):
    help = "Delete conversation history older than six months and rotate native sessions."

    def handle(self, *args: Any, **options: Any) -> None:
        counts = asyncio.run(_cleanup())
        self.stdout.write(f"purged_conversations={counts.purged_conversations}")
        self.stdout.write(f"pruned_turns={counts.pruned_turns}")
        self.stdout.write(f"cancelled_waiting_turns={counts.cancelled_waiting_turns}")
        self.stdout.write(f"successful_rotations={counts.successful_rotations}")
        self.stdout.write(f"bindings_requiring_recreation={counts.bindings_requiring_recreation}")
