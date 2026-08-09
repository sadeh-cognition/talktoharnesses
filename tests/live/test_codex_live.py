"""Opt-in live Codex create/resume tests.

Enable with TALKTOHARNESSES_LIVE_CODEX=1.
When enabled, missing SDK/auth/version is a failure rather than a skip.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.models import HarnessConfiguration, LaunchSnapshot
from talktoharnesses.providers.adapter import StartSessionRequest, TurnRequest
from talktoharnesses.providers.codex import CodexAdapter

pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_LIVE_CODEX") != "1",
    reason="set TALKTOHARNESSES_LIVE_CODEX=1 to run live Codex tests",
)


@pytest.mark.asyncio
async def test_live_codex_probe_and_create(tmp_path: Path) -> None:
    config = HarnessConfiguration(
        kind=HarnessKind.CODEX,
        working_directory=str(tmp_path),
        mode="workspace_write",
    )
    adapter = CodexAdapter()
    caps = await adapter.probe(config)
    launch = LaunchSnapshot(
        harness_version=caps.version,
        working_directory=str(tmp_path),
        adapter_version="2026.8.0.dev8",
        capabilities=caps,
    )
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=config,
            launch=launch,
        )
    )
    try:
        assert session.native_session_id
        await adapter.submit(
            session,
            TurnRequest(turn_id=uuid4(), prompt="Reply with the single word: pong"),
        )
    finally:
        await adapter.close(session)
