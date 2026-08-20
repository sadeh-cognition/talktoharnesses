"""Opt-in live Cursor create/resume/interaction tests.

Enable with TALKTOHARNESSES_LIVE_CURSOR=1 and TALKTOHARNESSES_CURSOR_EXECUTABLE.
When enabled, missing auth/binary/version is a failure rather than a skip.
"""

from __future__ import annotations

import os
from uuid import UUID

import pytest
from tests.live.helpers import (
    LiveHttp,
    LiveStream,
    require_executable,
    run_live_gate,
    unique_prompt,
)

from talktoharnesses.client import AsyncTalkToHarnessesClient
from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.models import HarnessConfiguration
from talktoharnesses.providers.cursor.compatibility import load_cursor_compatibility

pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_LIVE_CURSOR") != "1",
    reason="set TALKTOHARNESSES_LIVE_CURSOR=1 to run live Cursor tests",
)

_PINNED_RELEASE = "cursor-2026.08.04-aaa8809"
_MODEL = "composer-2.5[fast=false]"
_MODE = "ask"


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


async def test_live_cursor_create_resume_interaction(live_http: LiveHttp) -> None:
    await run_live_gate(
        live_http,
        configuration=HarnessConfiguration(
            kind=HarnessKind.CURSOR,
            executable_path=require_executable("TALKTOHARNESSES_CURSOR_EXECUTABLE"),
            working_directory=str(live_http.workspace),
            model=_MODEL,
            mode=_MODE,
        ),
        releases=load_cursor_compatibility().releases,
        expected_release_id=_PINNED_RELEASE,
        after_create=_after_create,
    )
