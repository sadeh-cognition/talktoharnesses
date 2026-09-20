"""Provider credential-proxy create/resume gate, enabled independently of tool tests."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from tests.live.helpers import (
    LiveHttp,
    LiveStream,
    assistant_text,
    isolated_sandbox_environment,
    resolve_interaction,
    scoped_configuration,
)

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.events import ConversationEvent
from talktoharnesses.domain.models import HarnessConfiguration
from talktoharnesses.remote.sandbox_auth import AUTH_FILE_DEFAULTS

pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_POLICY_PROVIDERS") != "1",
    reason="set TALKTOHARNESSES_POLICY_PROVIDERS=1 (uses host provider logins through gateway)",
)


@pytest.fixture(params=list(HarnessKind), ids=lambda kind: kind.value)
def provider_scope(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[HarnessKind]:
    kind = HarnessKind(request.param)
    spec = AUTH_FILE_DEFAULTS[kind]
    with isolated_sandbox_environment(
        monkeypatch,
        tmp_path_factory,
        kind=kind,
        auth_environment_variable=spec.environment_variable,
        default_auth_path=Path(spec.default_relative_path),
    ):
        yield kind


async def test_provider_start_and_resume_with_proxied_credentials(
    provider_scope: HarnessKind,
    live_http: LiveHttp,
) -> None:
    client = live_http.client
    async with asyncio.timeout(150):
        configuration = await scoped_configuration(
            client,
            HarnessConfiguration(
                kind=provider_scope,
                working_directory=str(live_http.workspace),
            ),
        )
        harness = await client.create_harness(
            name="policy compatibility", configuration=configuration
        )
        await client.probe_harness(harness.id, timeout=90)
        snapshot = await client.create_conversation(harness.id)
        conversation_id = snapshot.detail.conversation.id
        sequence = snapshot.sequence

        async def resolve(event: ConversationEvent) -> None:
            await resolve_interaction(client, conversation_id, event)

        for attempt in range(2):
            items = client.stream_conversation_events(conversation_id, after_sequence=sequence)
            stream = LiveStream(items, resolve)
            try:
                reply = "SANDBOX_FIRST" if attempt == 0 else "SANDBOX_RESUMED"
                submitted = await client.submit_turn(
                    conversation_id,
                    prompt=f"Reply with exactly {reply}. Do not run tools or edit files.",
                    idempotency_key=str(uuid4()),
                )
                events = await stream.collect_turn(submitted.turn.id, timeout=90)
                assert reply in assistant_text(events), (
                    f"attempt {attempt}: {assistant_text(events)!r}"
                )
                sequence = events[-1].sequence
            finally:
                closer = getattr(items, "aclose", None)
                if closer is not None:
                    await closer()
            if attempt == 0:
                await live_http.close_runtime(conversation_id)
        detail = await client.get_conversation(conversation_id)
        assert detail.detail.sandbox_policy == configuration.sandbox_policy
