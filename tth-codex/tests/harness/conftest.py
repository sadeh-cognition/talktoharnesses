"""Fixtures for adapter tests that drive the real broker over the fake SDK."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from tth_types.adapter import HarnessSession, StartSessionRequest

from tests.harness.fakes import FakeCodex, harness_config, launch_snapshot
from tth_codex.harness.adapter import CodexAdapter
from tth_codex.harness.compatibility import match_release


@pytest.fixture
async def broker() -> AsyncIterator[tuple[CodexAdapter, HarnessSession]]:
    """A started adapter with an active turn, ready to broker server requests."""
    adapter = CodexAdapter(client_factory=FakeCodex)
    # Exercise the real broker with an offline compatible SDK transport.
    adapter._release = match_release(  # pyright: ignore[reportPrivateUsage]
        sdk_version="0.154.0", runtime_version="0.154.0", platform="linux"
    )
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=harness_config(),
            launch=launch_snapshot(),
        )
    )
    # Keep an active turn without racing the fake stream's terminal event.
    adapter._normalizer.begin_turn(uuid4())  # pyright: ignore[reportPrivateUsage]
    try:
        yield adapter, session
    finally:
        await adapter.close(session)
