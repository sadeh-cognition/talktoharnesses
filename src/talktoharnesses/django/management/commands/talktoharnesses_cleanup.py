"""Externally scheduled retention pass.

Builds the normal Django composition without starting command workers, runs one
:func:`run_cleanup` pass (or a read-only dry-run preview), and prints its
counts. It never reads, changes, or deletes workspace files
(``docs/phase8.md`` Work Package 5 / ``docs/phase11.md`` Work Package 3).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from django.core.management.base import BaseCommand

from talktoharnesses.application.retention import (
    CleanupCounts,
    DryRunCounts,
    preview_cleanup,
    run_cleanup,
)
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


async def _dry_run() -> DryRunCounts:
    return await preview_cleanup(DjangoPersistence(), _utc_clock)


class Command(BaseCommand):
    help = (
        "Delete conversation history older than each owner's retention period "
        "and rotate native sessions."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print aggregate eligible counts without mutating state.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options.get("dry_run"):
            counts = asyncio.run(_dry_run())
            self.stdout.write(f"soft_deleted_conversations={counts.soft_deleted_conversations}")
            self.stdout.write(f"history_conversations={counts.history_conversations}")
            self.stdout.write(f"terminal_turns={counts.terminal_turns}")
            self.stdout.write(f"waiting_turns={counts.waiting_turns}")
            return
        counts = asyncio.run(_cleanup())
        self.stdout.write(f"purged_conversations={counts.purged_conversations}")
        self.stdout.write(f"pruned_turns={counts.pruned_turns}")
        self.stdout.write(f"cancelled_waiting_turns={counts.cancelled_waiting_turns}")
        self.stdout.write(f"successful_rotations={counts.successful_rotations}")
        self.stdout.write(f"bindings_requiring_recreation={counts.bindings_requiring_recreation}")
