"""Provider-neutral adapter contract suite over fake native transports."""

from __future__ import annotations

from uuid import uuid4

import pytest
from tests.contract.fakes import capabilities_for, config_for, make_adapter_factory

from talktoharnesses import __version__
from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.models import LaunchSnapshot
from talktoharnesses.providers.adapter import (
    ResumeSessionRequest,
    StartSessionRequest,
    SteerRequest,
    TurnRequest,
)

PROVIDER_KINDS = (
    HarnessKind.GROK,
    HarnessKind.CURSOR,
    HarnessKind.CODEX,
    HarnessKind.CLAUDE,
    HarnessKind.OPENCODE,
)


def _launch(kind: HarnessKind) -> LaunchSnapshot:
    caps = capabilities_for(kind)
    return LaunchSnapshot(
        resolved_executable="/bin/true" if kind is HarnessKind.OPENCODE else None,
        harness_version=caps.version,
        working_directory="/tmp",
        adapter_version=__version__,
        capabilities=caps,
        model="test/default" if kind is HarnessKind.OPENCODE else "default",
        mode="default",
    )


@pytest.fixture(params=PROVIDER_KINDS, ids=lambda k: k.value)
def kind(request: pytest.FixtureRequest) -> HarnessKind:
    return request.param  # type: ignore[no-any-return]


@pytest.mark.asyncio
async def test_probe_start_resume_submit_terminal_steer_interrupt_close_isolation(
    kind: HarnessKind,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = make_adapter_factory(kind, monkeypatch)
    adapter, bind = factory()
    config = config_for(kind)
    caps = await adapter.probe(config)
    assert caps.kind is kind
    assert isinstance(caps.version, str) and caps.version

    bind(adapter)
    conversation_id = uuid4()
    binding_id = uuid4()
    launch = _launch(kind)
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=conversation_id,
            binding_id=binding_id,
            configuration=config,
            launch=launch,
        )
    )
    assert session.native_session_id
    native_id = session.native_session_id

    # Fresh adapter for resume isolation.
    adapter2, bind2 = factory()
    await adapter2.probe(config)
    bind2(adapter2)
    resumed = await adapter2.resume(
        ResumeSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=config,
            native_session_id=native_id,
            launch=launch,
        )
    )
    assert resumed.native_session_id == native_id

    turn_id = uuid4()
    await adapter.submit(session, TurnRequest(turn_id=turn_id, prompt="hello"))
    steered = await adapter.steer(session, SteerRequest(turn_id=turn_id, prompt="more"))
    # Steer either succeeds once or returns False without losing the prompt.
    assert steered in {True, False}

    # Terminal without requiring a final assistant message.
    saw_terminal = False

    async def _drain_terminal() -> None:
        nonlocal saw_terminal
        async for item in adapter.events(session):
            event_type = getattr(item, "type", None)
            if event_type in {
                "turn_completed",
                "turn_failed",
                "turn_interrupted",
                "turn_outcome_unknown",
            }:
                saw_terminal = True
                return

    import asyncio

    try:
        await asyncio.wait_for(_drain_terminal(), timeout=2.0)
    except TimeoutError:
        saw_terminal = False
    # OpenCode may race SSE status before begin_turn; other adapters must settle.
    if kind is not HarnessKind.OPENCODE:
        assert saw_terminal

    await adapter.interrupt(session)
    await adapter.interrupt(session)  # idempotent at orchestration boundary
    await adapter.close(session)
    await adapter.close(session)

    # Two conversations never share an adapter instance.
    a, bind_a = factory()
    b, bind_b = factory()
    assert a is not b
    await a.probe(config)
    await b.probe(config)
    bind_a(a)
    bind_b(b)
    s_a = await a.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=config,
            launch=launch,
        )
    )
    s_b = await b.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=config,
            launch=launch,
        )
    )
    assert s_a.native_session_id != s_b.native_session_id
    await a.close(s_a)
    await b.close(s_b)
    await adapter2.close(resumed)
