"""Grok adapter live usage notification tests."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from uuid import uuid4

import pytest
from tests.contract.fakes import _FakeAcpProcess  # pyright: ignore[reportPrivateUsage]

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.events import (
    CostUpdatedPayload,
    TurnCompletedPayload,
    UsageUpdatedPayload,
)
from talktoharnesses.domain.models import HarnessConfiguration, LaunchSnapshot
from talktoharnesses.providers.adapter import StartSessionRequest, TurnRequest
from talktoharnesses.providers.grok.adapter import GrokAdapter
from talktoharnesses.providers.grok.compatibility import match_release


class _UsageAcpProcess(_FakeAcpProcess):
    async def _respond(self, msg: dict[str, Any]) -> None:
        if msg.get("method") == "session/prompt":
            params_obj = msg.get("params")
            params = cast(dict[str, object], params_obj) if isinstance(params_obj, dict) else {}
            notification = {
                "jsonrpc": "2.0",
                "method": "_x.ai/session_notification",
                "params": {
                    "sessionId": params.get("sessionId"),
                    "update": {
                        "sessionUpdate": "turn_completed",
                        "usage": {
                            "inputTokens": 10,
                            "outputTokens": 3,
                            "totalTokens": 13,
                            "cachedReadTokens": 2,
                            "costUsdTicks": 10_000_000_000,
                        },
                    },
                },
            }
            await self._stdout_q.put(  # pyright: ignore[reportPrivateUsage]
                (json.dumps(notification) + "\n").encode()
            )
        await super()._respond(msg)


@pytest.mark.asyncio
async def test_live_xai_usage_is_emitted_before_prompt_terminal() -> None:
    release = match_release("grok 1.0.5 (5115b46bc9) [stable]", platform="linux")
    adapter = GrokAdapter()
    adapter._release = release  # pyright: ignore[reportPrivateUsage]
    adapter._capabilities = release.to_harness_capabilities()  # pyright: ignore[reportPrivateUsage]
    process = _UsageAcpProcess(agent_version="1.0.5")
    adapter.bind_process(process)  # type: ignore[arg-type]
    configuration = HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp")
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=configuration,
            launch=LaunchSnapshot(
                harness_version="1.0.5",
                working_directory="/tmp",
                adapter_version="test",
                capabilities=release.to_harness_capabilities(),
            ),
        )
    )
    turn_id = uuid4()
    await adapter.submit(session, TurnRequest(turn_id=turn_id, prompt="hello"))

    stream = adapter.events(session)
    events = [await asyncio.wait_for(anext(stream), timeout=1) for _ in range(3)]

    assert [type(event) for event in events] == [
        UsageUpdatedPayload,
        CostUpdatedPayload,
        TurnCompletedPayload,
    ]
    usage = events[0]
    assert isinstance(usage, UsageUpdatedPayload)
    assert usage.turn_id == turn_id
    assert usage.input_tokens == 10
    assert usage.output_tokens == 3
    assert usage.total_tokens == 13
    assert usage.cached_input_tokens == 2
    cost = events[1]
    assert isinstance(cost, CostUpdatedPayload)
    assert cost.turn_id == turn_id
    assert cost.cost == "1"
    assert cost.currency == "USD"
    await adapter.close(session)
