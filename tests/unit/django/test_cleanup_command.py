"""Django management command entry for talktoharnesses_cleanup."""

from __future__ import annotations

from datetime import UTC
from io import StringIO
from unittest.mock import AsyncMock, patch

import pytest
from django.core.management import call_command

from talktoharnesses.application.retention import CleanupCounts


@pytest.mark.django_db(transaction=True)
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


@pytest.mark.django_db(transaction=True)
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
