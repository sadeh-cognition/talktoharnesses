"""Django management command entry for talktoharnesses_cleanup."""

from __future__ import annotations

from datetime import UTC
from io import StringIO
from unittest.mock import AsyncMock, patch

import pytest
from django.core.management import call_command

from talktoharnesses.application.retention import CleanupCounts, DryRunCounts


def test_talktoharnesses_cleanup_prints_counts() -> None:
    counts = CleanupCounts(
        purged_conversations=1,
        pruned_turns=2,
        cancelled_waiting_turns=3,
        successful_rotations=4,
        bindings_requiring_recreation=5,
    )
    with patch(
        "talktoharnesses.django.management.commands.talktoharnesses_cleanup._cleanup",
        new=AsyncMock(return_value=counts),
    ):
        out = StringIO()
        call_command("talktoharnesses_cleanup", stdout=out)
    text = out.getvalue()
    assert "purged_conversations=1" in text
    assert "pruned_turns=2" in text
    assert "cancelled_waiting_turns=3" in text
    assert "successful_rotations=4" in text
    assert "bindings_requiring_recreation=5" in text


def test_talktoharnesses_cleanup_dry_run_prints_preview_counts() -> None:
    counts = DryRunCounts(
        soft_deleted_conversations=2,
        history_conversations=3,
        terminal_turns=4,
        waiting_turns=1,
    )
    with patch(
        "talktoharnesses.django.management.commands.talktoharnesses_cleanup._dry_run",
        new=AsyncMock(return_value=counts),
    ) as dry_run:
        out = StringIO()
        call_command("talktoharnesses_cleanup", "--dry-run", stdout=out)
    assert dry_run.await_count == 1
    text = out.getvalue()
    assert "soft_deleted_conversations=2" in text
    assert "history_conversations=3" in text
    assert "terminal_turns=4" in text
    assert "waiting_turns=1" in text
    assert "purged_conversations=" not in text
    assert "pruned_turns=" not in text


@pytest.mark.asyncio
async def test_cleanup_composition_uses_default_registry() -> None:
    from talktoharnesses.django.management.commands import talktoharnesses_cleanup as cmd
    from talktoharnesses.domain.enums import HarnessKind

    with (
        patch.object(cmd, "run_cleanup", new=AsyncMock(return_value=CleanupCounts())) as run,
        patch.object(cmd, "RuntimeManager") as runtime_cls,
    ):
        await cmd._cleanup()  # pyright: ignore[reportPrivateUsage]
    assert run.await_count == 1
    registry = runtime_cls.call_args.args[1]
    assert HarnessKind.CODEX in registry.kinds()
    assert HarnessKind.GROK in registry.kinds()


def test_clock_is_utc() -> None:
    from talktoharnesses.django.management.commands.talktoharnesses_cleanup import (
        _utc_clock,  # pyright: ignore[reportPrivateUsage]
    )

    now = _utc_clock()
    assert now.tzinfo == UTC
