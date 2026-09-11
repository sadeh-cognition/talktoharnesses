"""Opt-in live Cursor gate through TTH-managed Docker sandboxing.

Enable with ``TALKTOHARNESSES_LIVE_CURSOR_SANDBOX=1``. The fixture configures
an isolated Docker sandbox and removes ``CURSOR_API_KEY``, so the full
create/resume journey also proves host-file authentication. TTH owns
credential seeding, routing, and split authentication.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from tests.live.helpers import (
    LiveHttp,
    LiveStream,
    assert_rtk_rewrite,
    isolated_sandbox_environment,
    run_live_gate,
    unique_prompt,
)

from talktoharnesses.client import AsyncTalkToHarnessesClient
from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.models import HarnessConfiguration

pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_LIVE_CURSOR_SANDBOX") != "1",
    reason="set TALKTOHARNESSES_LIVE_CURSOR_SANDBOX=1 to run the live Cursor sandbox test",
)

_MODEL = "composer-2.5[fast=false]"
_MODE = "agent"


@pytest.fixture(autouse=True)
def cursor_sandbox_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    with isolated_sandbox_environment(
        monkeypatch,
        tmp_path_factory,
        kind=HarnessKind.CURSOR,
        auth_environment_variable="TTH_SANDBOX_CURSOR_AUTH_FILE",
        default_auth_path=Path(".config/cursor/auth.json"),
        credential_environment_variable="CURSOR_API_KEY",
    ):
        yield


async def _assert_baseline(client: AsyncTalkToHarnessesClient, conversation_id: UUID) -> None:
    snapshot = await client.get_conversation(conversation_id)
    assert snapshot.detail.model == _MODEL
    assert snapshot.detail.mode == _MODE


async def _after_create(
    stream: LiveStream,
    client: AsyncTalkToHarnessesClient,
    conversation_id: UUID,
) -> None:
    await _assert_baseline(client, conversation_id)
    override = await client.submit_turn(
        conversation_id,
        prompt=unique_prompt("override-turn"),
        idempotency_key=f"cursor-override-{conversation_id}",
        model="composer-2.5[fast=true]",
    )
    await stream.collect_turn(override.turn.id)
    await _assert_baseline(client, conversation_id)
    restore = await client.submit_turn(
        conversation_id,
        prompt=unique_prompt("restore-turn"),
        idempotency_key=f"cursor-restore-{conversation_id}",
    )
    await stream.collect_turn(restore.turn.id)
    await _assert_baseline(client, conversation_id)
    await assert_rtk_rewrite(stream, client, conversation_id)


async def test_live_cursor_through_tth_docker_sandbox(live_http: LiveHttp) -> None:
    await run_live_gate(
        live_http,
        configuration=HarnessConfiguration(
            kind=HarnessKind.CURSOR,
            working_directory=str(live_http.workspace),
            model=_MODEL,
            mode=_MODE,
        ),
        after_create=_after_create,
    )
