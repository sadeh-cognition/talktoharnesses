"""Proxy ↔ tth-claude split integration through the Docker sandbox path.

The proxy spawns the tth-claude sandbox on demand — building the image locally
when absent — and the containerized split boots the echo adapter (no SDK or
credentials) via the forwarded ``TTH_SPLIT_ADAPTER_FACTORY``. This is the
end-to-end gate for on-demand sandbox provisioning.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from tests.live.helpers import TERMINAL_TYPES, LiveHttp, isolated_sandbox_environment

from talktoharnesses.client import APIError, AsyncTalkToHarnessesClient
from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.events import ConversationEvent, event_turn_id
from talktoharnesses.domain.models import HarnessConfiguration, HarnessProbeProjection

# Opt-in like every live gate: tests/live session fixtures repoint the default
# database, so this must never run inside the default suite. Requires Docker.
pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_SPLIT_INTEGRATION") != "1",
    reason="set TALKTOHARNESSES_SPLIT_INTEGRATION=1",
)


async def probe_until_ready(
    client: AsyncTalkToHarnessesClient,
    harness_id: UUID,
    *,
    deadline_seconds: float = 1500.0,
) -> HarnessProbeProjection:
    """Probe, retrying while the sandbox is being prepared (image build/boot)."""
    deadline = asyncio.get_running_loop().time() + deadline_seconds
    while True:
        try:
            return await client.probe_harness(harness_id, timeout=120.0)
        except APIError as exc:
            if exc.code != "sandbox_preparing":
                raise
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("sandbox was still preparing at the deadline") from exc
            await asyncio.sleep(5.0)


@pytest.fixture(autouse=True)
def claude_echo_sandbox(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    # Synthetic credential file: the echo adapter needs none, but sandbox
    # creation seeds whatever auth file is configured for the kind.
    auth_file = tmp_path_factory.mktemp("claude-echo-auth") / ".credentials.json"
    auth_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("TTH_SANDBOX_CLAUDE_AUTH_FILE", str(auth_file))
    # Forward the factory override into the container instead of a credential.
    monkeypatch.setenv("TTH_SANDBOX_ENV_CLAUDE", "TTH_SPLIT_ADAPTER_FACTORY")
    monkeypatch.setenv(
        "TTH_SPLIT_ADAPTER_FACTORY", "tth_claude.testing:echo_adapter_factory"
    )
    with isolated_sandbox_environment(
        monkeypatch,
        tmp_path_factory,
        kind=HarnessKind.CLAUDE,
        auth_environment_variable="TTH_SANDBOX_CLAUDE_AUTH_FILE",
        default_auth_path=Path(".claude/.credentials.json"),
        credential_environment_variable="ANTHROPIC_API_KEY",
    ):
        yield


async def test_proxy_journey_through_sandboxed_split(live_http: LiveHttp) -> None:
    client = live_http.client

    harness = await client.create_harness(
        name="split-claude",
        configuration=HarnessConfiguration(
            kind=HarnessKind.CLAUDE,
            working_directory=str(live_http.workspace),
        ),
    )
    probe = await probe_until_ready(client, harness.id)
    assert probe.capabilities.version == "0.0.0+echo"
    assert probe.capabilities.supports_resume is True

    snapshot = await client.create_conversation(harness.id, title="split-gate")
    conversation_id = snapshot.detail.conversation.id

    items = client.stream_conversation_events(conversation_id)
    created = await client.submit_turn(
        conversation_id,
        prompt="hello split",
        idempotency_key=f"split-{uuid4().hex}",
    )
    turn_id = created.turn.id

    collected: list[ConversationEvent] = []

    async def _drain() -> None:
        async for item in items:
            if not isinstance(item, ConversationEvent):
                continue
            collected.append(item)
            if item.type in TERMINAL_TYPES and event_turn_id(item) == turn_id:
                return
        raise AssertionError("stream ended before the turn terminated")

    await asyncio.wait_for(_drain(), timeout=60.0)

    terminal = [event for event in collected if event.type in TERMINAL_TYPES]
    assert terminal[-1].type == "turn_completed"
    messages = [
        event for event in collected if event.type == "assistant_message_completed"
    ]
    assert messages, "no assistant message arrived through the split"
    text = getattr(messages[-1].payload, "text", "")
    assert text == "echo: hello split"
