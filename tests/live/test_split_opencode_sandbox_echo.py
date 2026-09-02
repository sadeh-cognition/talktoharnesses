"""Proxy ↔ tth-opencode split integration through the Docker sandbox path.

The proxy spawns the tth-opencode sandbox on demand — building the image
locally when absent — and the containerized split boots the process-spawning
echo adapter (no SDK or credentials) via the forwarded
``TTH_SPLIT_ADAPTER_FACTORY``, proving process adoption through a sandboxed
split.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from tests.live.helpers import TERMINAL_TYPES, LiveHttp, isolated_sandbox_environment
from tests.live.test_split_claude_sandbox_echo import probe_until_ready

from talktoharnesses.django.asgi import get_service
from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.events import ConversationEvent, event_turn_id
from talktoharnesses.domain.models import HarnessConfiguration

# Opt-in like every live gate: tests/live session fixtures repoint the default
# database, so this must never run inside the default suite. Requires Docker.
pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_SPLIT_INTEGRATION") != "1",
    reason="set TALKTOHARNESSES_SPLIT_INTEGRATION=1",
)


@pytest.fixture(autouse=True)
def opencode_echo_sandbox(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    # Synthetic credential file: the echo adapter needs none, but sandbox
    # creation seeds whatever auth file is configured for the kind.
    auth_file = tmp_path_factory.mktemp("opencode-echo-auth") / "auth.json"
    auth_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("TTH_SANDBOX_OPENCODE_AUTH_FILE", str(auth_file))
    # Forward the factory override into the container instead of a credential.
    monkeypatch.setenv("TTH_SANDBOX_ENV_OPENCODE", "TTH_SPLIT_ADAPTER_FACTORY")
    monkeypatch.setenv(
        "TTH_SPLIT_ADAPTER_FACTORY", "tth_opencode.testing:spawn_echo_adapter_factory"
    )
    with isolated_sandbox_environment(
        monkeypatch,
        tmp_path_factory,
        kind=HarnessKind.OPENCODE,
        auth_environment_variable="TTH_SANDBOX_OPENCODE_AUTH_FILE",
        default_auth_path=Path(".local/share/opencode/auth.json"),
        credential_environment_variable="OPENCODE_API_KEY",
    ):
        yield


async def test_process_bound_journey_through_sandboxed_split(live_http: LiveHttp) -> None:
    client = live_http.client

    harness = await client.create_harness(
        name="split-opencode",
        configuration=HarnessConfiguration(
            kind=HarnessKind.OPENCODE,
            working_directory=str(live_http.workspace),
        ),
    )
    probe = await probe_until_ready(client, harness.id)
    assert probe.capabilities.version == "0.0.0+echo"

    snapshot = await client.create_conversation(harness.id, title="split-opencode-gate")
    conversation_id = snapshot.detail.conversation.id

    items = client.stream_conversation_events(conversation_id)
    created = await client.submit_turn(
        conversation_id,
        prompt="hello process",
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
    messages = [event for event in collected if event.type == "assistant_message_completed"]
    assert messages and getattr(messages[-1].payload, "text", "") == "echo: hello process"

    # The proxy adopted the split-supervised process: a containerless pid from
    # the split's spawn is recorded on the managed runtime.
    runtime = get_service()._runtime  # pyright: ignore[reportPrivateUsage]
    managed = runtime.get_runtime(conversation_id)
    assert managed is not None
    assert managed.process is not None
    assert managed.process.pid is not None
    assert managed.process_record.pid == managed.process.pid
