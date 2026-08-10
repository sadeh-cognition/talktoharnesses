"""Cursor adapter lifecycle and model/mode selection tests."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from tests.contract.fakes import _FakeAcpProcess  # pyright: ignore[reportPrivateUsage]

from talktoharnesses.domain.enums import ApprovalDecision, ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import TurnFailedPayload, TurnOutcomeUnknownPayload
from talktoharnesses.domain.models import (
    HarnessCapabilities,
    HarnessConfiguration,
    InteractionAnswer,
    LaunchSnapshot,
)
from talktoharnesses.providers.acp.jsonrpc import JsonRpcRemoteError
from talktoharnesses.providers.adapter import (
    HarnessInteractionRequest,
    HarnessSession,
    ResumeSessionRequest,
    StartSessionRequest,
    TurnRequest,
)
from talktoharnesses.providers.cursor import adapter as cursor_adapter_mod
from talktoharnesses.providers.cursor.adapter import CursorAdapter
from talktoharnesses.providers.cursor.argv import build_cursor_argv
from talktoharnesses.providers.cursor.compatibility import match_release

_CursorModelSelection = cursor_adapter_mod._CursorModelSelection  # pyright: ignore[reportPrivateUsage]
_parse_model_selector = cursor_adapter_mod._parse_model_selector  # pyright: ignore[reportPrivateUsage]


def _as_str_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in cast(dict[object, object], value).items()}


def _config(
    *,
    model: str | None = "composer-2.5[fast=false]",
    mode: str | None = "ask",
) -> HarnessConfiguration:
    return HarnessConfiguration(
        kind=HarnessKind.CURSOR,
        executable_path="/bin/true",
        working_directory="/tmp",
        model=model,
        mode=mode,
    )


def _launch() -> LaunchSnapshot:
    return LaunchSnapshot(
        resolved_executable="/bin/true",
        harness_version="2026.08.04-aaa8809",
        working_directory="/tmp",
        adapter_version="test",
        capabilities=HarnessCapabilities(
            kind=HarnessKind.CURSOR,
            version="2026.08.04-aaa8809",
            supports_resume=True,
            supports_interrupt=True,
            supports_multi_interaction=True,
        ),
        model="composer-2.5[fast=false]",
        mode="ask",
    )


async def _probed_adapter(
    proc: _FakeAcpProcess | None = None,
) -> tuple[CursorAdapter, _FakeAcpProcess]:
    adapter = CursorAdapter()
    release = match_release("2026.08.04-aaa8809", platform="linux")
    adapter._release = release  # pyright: ignore[reportPrivateUsage]
    adapter._capabilities = release.to_harness_capabilities()  # pyright: ignore[reportPrivateUsage]
    process = proc or _FakeAcpProcess(
        agent_name="cursor",
        agent_version="2026.08.04-aaa8809",
    )
    adapter.bind_process(process)  # type: ignore[arg-type]
    return adapter, process


def _setter_calls(proc: _FakeAcpProcess) -> list[dict[str, Any]]:
    return [msg for msg in proc.requests if msg.get("method") == "session/set_config_option"]


def _setter_pairs(proc: _FakeAcpProcess) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for msg in _setter_calls(proc):
        params = _as_str_dict(msg.get("params"))
        pairs.append((str(params.get("configId")), str(params.get("value"))))
    return pairs


# ---------------------------------------------------------------------------
# Selector parsing
# ---------------------------------------------------------------------------


def test_parse_simple_model() -> None:
    assert _parse_model_selector("composer-2.5") == _CursorModelSelection("composer-2.5")


def test_parse_parameterized_model() -> None:
    result = _parse_model_selector("gpt-5.6-sol[context=272k,reasoning=high,fast=false]")
    assert result == _CursorModelSelection(
        "gpt-5.6-sol",
        (("context", "272k"), ("reasoning", "high"), ("fast", "false")),
    )


def test_parse_whitespace_normalization() -> None:
    result = _parse_model_selector("  composer-2.5[ fast = false ]  ")
    assert result == _CursorModelSelection("composer-2.5", (("fast", "false"),))


def test_parse_auto_to_default() -> None:
    assert _parse_model_selector("auto") == _CursorModelSelection("default")
    assert _parse_model_selector("auto[]") == _CursorModelSelection("default")


def test_parse_empty_brackets() -> None:
    assert _parse_model_selector("default[]") == _CursorModelSelection("default")


@pytest.mark.parametrize(
    "selector",
    [
        "",
        "   ",
        "[]",
        "  [fast=false]",
    ],
)
def test_parse_missing_model_id(selector: str) -> None:
    with pytest.raises(DomainError) as exc:
        _parse_model_selector(selector)
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_parse_missing_closing_bracket() -> None:
    with pytest.raises(DomainError) as exc:
        _parse_model_selector("composer-2.5[fast=false")
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_parse_trailing_after_bracket() -> None:
    with pytest.raises(DomainError) as exc:
        _parse_model_selector("composer-2.5[fast=false]x")
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_parse_nested_or_repeated_brackets() -> None:
    with pytest.raises(DomainError) as exc:
        _parse_model_selector("composer-2.5[a=b][c=d]")
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
    with pytest.raises(DomainError) as exc2:
        _parse_model_selector("composer-2.5[a=b[c=d]]")
    assert exc2.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
    with pytest.raises(DomainError) as exc3:
        _parse_model_selector("composer-2.5[fast=false]]")
    assert exc3.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_parse_parameter_without_equals() -> None:
    with pytest.raises(DomainError) as exc:
        _parse_model_selector("composer-2.5[fast]")
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_parse_empty_parameter_id() -> None:
    with pytest.raises(DomainError) as exc:
        _parse_model_selector("composer-2.5[=false]")
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_parse_empty_parameter_value() -> None:
    with pytest.raises(DomainError) as exc:
        _parse_model_selector("composer-2.5[fast=]")
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_parse_duplicate_parameter_ids() -> None:
    with pytest.raises(DomainError) as exc:
        _parse_model_selector("composer-2.5[fast=false,fast=true]")
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
    assert exc.value.details.get("parameter_id") == "fast"


def test_build_cursor_argv_always_acp() -> None:
    assert build_cursor_argv() == ("acp",)


# ---------------------------------------------------------------------------
# Adapter lifecycle with configuration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_requests_parameterized_model_picker() -> None:
    adapter, proc = await _probed_adapter()
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(),
            launch=_launch(),
        )
    )
    assert session.native_session_id
    init = next(msg for msg in proc.requests if msg.get("method") == "initialize")
    params = _as_str_dict(init.get("params"))
    assert params.get("clientCapabilities") == {
        "_meta": {"parameterizedModelPicker": True},
    }
    await adapter.close(session)


@pytest.mark.asyncio
async def test_start_applies_model_parameters_then_mode() -> None:
    adapter, proc = await _probed_adapter()
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(model="composer-2.5[fast=true]", mode="ask"),
            launch=_launch(),
        )
    )
    assert _setter_pairs(proc) == [
        ("model", "composer-2.5"),
        ("fast", "true"),
        ("mode", "ask"),
    ]
    assert session.model == "composer-2.5[fast=true]"
    assert session.mode == "ask"
    assert adapter._session_model_selection == _CursorModelSelection(  # pyright: ignore[reportPrivateUsage]
        "composer-2.5",
        (("fast", "true"),),
    )
    await adapter.close(session)


@pytest.mark.asyncio
async def test_resume_applies_authoritative_configuration() -> None:
    adapter, proc = await _probed_adapter()
    session = await adapter.resume(
        ResumeSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(model="gpt-5.6-sol[reasoning=high,fast=false]", mode="plan"),
            native_session_id="resume-session",
            launch=_launch(),
        )
    )
    assert session.native_session_id == "resume-session"
    # fast=false is the model default after selection, so it is not re-sent.
    assert _setter_pairs(proc) == [
        ("model", "gpt-5.6-sol"),
        ("reasoning", "high"),
        ("mode", "plan"),
    ]
    assert adapter._session_model_selection == _CursorModelSelection(  # pyright: ignore[reportPrivateUsage]
        "gpt-5.6-sol",
        (("context", "272k"), ("reasoning", "high"), ("fast", "false")),
    )
    await adapter.close(session)


@pytest.mark.asyncio
async def test_no_configured_values_capture_baseline_without_setters() -> None:
    adapter, proc = await _probed_adapter()
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(model=None, mode=None),
            launch=_launch(),
        )
    )
    assert _setter_calls(proc) == []
    assert adapter._session_model_selection == _CursorModelSelection("default")  # pyright: ignore[reportPrivateUsage]
    assert adapter._current_model_selection == adapter._session_model_selection  # pyright: ignore[reportPrivateUsage]
    await adapter.close(session)


@pytest.mark.asyncio
async def test_unknown_model_fails_before_session_return() -> None:
    adapter, proc = await _probed_adapter()
    with pytest.raises(DomainError) as exc:
        await adapter.start(
            StartSessionRequest(
                conversation_id=uuid4(),
                binding_id=uuid4(),
                configuration=_config(model="not-a-model", mode="ask"),
                launch=_launch(),
            )
        )
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
    assert adapter._session is None  # pyright: ignore[reportPrivateUsage]
    assert not any(msg.get("method") == "session/prompt" for msg in proc.requests)


@pytest.mark.asyncio
async def test_unknown_mode_fails_before_session_return() -> None:
    adapter, _proc = await _probed_adapter()
    with pytest.raises(DomainError) as exc:
        await adapter.start(
            StartSessionRequest(
                conversation_id=uuid4(),
                binding_id=uuid4(),
                configuration=_config(model="composer-2.5", mode="telepathy"),
                launch=_launch(),
            )
        )
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
    assert adapter._session is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_unknown_parameter_id_fails_after_model_before_prompt() -> None:
    adapter, proc = await _probed_adapter()
    with pytest.raises(DomainError) as exc:
        await adapter.start(
            StartSessionRequest(
                conversation_id=uuid4(),
                binding_id=uuid4(),
                configuration=_config(model="composer-2.5[max=true]", mode="ask"),
                launch=_launch(),
            )
        )
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
    assert ("model", "composer-2.5") in _setter_pairs(proc)
    assert not any(cfg == "max" for cfg, _ in _setter_pairs(proc))
    assert adapter._session is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_unsupported_parameter_value_fails() -> None:
    adapter, _proc = await _probed_adapter()
    with pytest.raises(DomainError) as exc:
        await adapter.start(
            StartSessionRequest(
                conversation_id=uuid4(),
                binding_id=uuid4(),
                configuration=_config(model="composer-2.5[fast=maybe]", mode="ask"),
                launch=_launch(),
            )
        )
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
    assert exc.value.details.get("config_id") == "fast"
    assert "maybe" in str(exc.value.details.get("value"))


@pytest.mark.asyncio
async def test_setter_jsonrpc_rejection_maps_to_provider_incompatible() -> None:
    adapter, proc = await _probed_adapter()

    original = proc._respond_set_config_option  # pyright: ignore[reportPrivateUsage]

    async def reject_mode(req_id: object, params: object) -> None:
        params_map = _as_str_dict(params)
        if params_map.get("configId") == "mode":
            await proc._reply_error(req_id, -32000, "rejected")  # pyright: ignore[reportPrivateUsage]
            return
        await original(req_id, params)

    proc._respond_set_config_option = reject_mode  # type: ignore[method-assign]
    with pytest.raises(DomainError) as exc:
        await adapter.start(
            StartSessionRequest(
                conversation_id=uuid4(),
                binding_id=uuid4(),
                configuration=_config(model="composer-2.5", mode="ask"),
                launch=_launch(),
            )
        )
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
    assert exc.value.details.get("config_id") == "mode"
    assert exc.value.details.get("remote_code") == -32000


@pytest.mark.asyncio
async def test_malformed_setter_response_maps_to_protocol_error() -> None:
    adapter, proc = await _probed_adapter()

    async def bad_reply(req_id: object, params: object) -> None:
        del params
        await proc._reply(req_id, {"configOptions": "nope"})  # pyright: ignore[reportPrivateUsage]

    proc._respond_set_config_option = bad_reply  # type: ignore[method-assign]
    with pytest.raises(DomainError) as exc:
        await adapter.start(
            StartSessionRequest(
                conversation_id=uuid4(),
                binding_id=uuid4(),
                configuration=_config(model="composer-2.5", mode=None),
                launch=_launch(),
            )
        )
    assert exc.value.code is ErrorCode.PROTOCOL_ERROR


@pytest.mark.asyncio
async def test_setter_wrong_current_value_maps_to_protocol_error() -> None:
    adapter, proc = await _probed_adapter()

    async def wrong_current(req_id: object, params: object) -> None:
        params_map = _as_str_dict(params)
        config_id = params_map.get("configId")
        value = params_map.get("value")
        # Apply state but report a stale currentValue for the requested option.
        if config_id == "model" and isinstance(value, str):
            proc._reset_cursor_params_for_model(value)  # pyright: ignore[reportPrivateUsage]
        options = proc._cursor_config_options()  # pyright: ignore[reportPrivateUsage]
        for item in options:
            if item["id"] == config_id:
                item["currentValue"] = "stale-mismatch"
        await proc._reply(req_id, {"configOptions": options})  # pyright: ignore[reportPrivateUsage]

    proc._respond_set_config_option = wrong_current  # type: ignore[method-assign]
    with pytest.raises(DomainError) as exc:
        await adapter.start(
            StartSessionRequest(
                conversation_id=uuid4(),
                binding_id=uuid4(),
                configuration=_config(model="composer-2.5", mode=None),
                launch=_launch(),
            )
        )
    assert exc.value.code is ErrorCode.PROTOCOL_ERROR


@pytest.mark.asyncio
async def test_turn_override_applied_before_prompt() -> None:
    adapter, proc = await _probed_adapter()
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(model="composer-2.5[fast=false]", mode="ask"),
            launch=_launch(),
        )
    )
    proc.requests.clear()
    turn_id = uuid4()
    await adapter.submit(
        session,
        TurnRequest(turn_id=turn_id, prompt="hi", model="composer-2.5[fast=true]"),
    )
    methods = [msg.get("method") for msg in proc.requests]
    assert "session/set_config_option" in methods
    assert "session/prompt" in methods
    assert methods.index("session/set_config_option") < methods.index("session/prompt")
    assert _setter_pairs(proc) == [("fast", "true")]
    assert adapter._current_model_selection == _CursorModelSelection(  # pyright: ignore[reportPrivateUsage]
        "composer-2.5",
        (("fast", "true"),),
    )
    assert adapter._session_model_selection == _CursorModelSelection(  # pyright: ignore[reportPrivateUsage]
        "composer-2.5",
        (("fast", "false"),),
    )
    await adapter.close(session)


@pytest.mark.asyncio
async def test_second_turn_without_override_restores_baseline() -> None:
    adapter, proc = await _probed_adapter()
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(model="composer-2.5[fast=false]", mode="ask"),
            launch=_launch(),
        )
    )
    await adapter.submit(
        session,
        TurnRequest(turn_id=uuid4(), prompt="one", model="composer-2.5[fast=true]"),
    )
    # Drain prompt watcher briefly.
    await asyncio.sleep(0.05)
    proc.requests.clear()
    await adapter.submit(session, TurnRequest(turn_id=uuid4(), prompt="two"))
    assert _setter_pairs(proc) == [("fast", "false")]
    assert adapter._current_model_selection == adapter._session_model_selection  # pyright: ignore[reportPrivateUsage]
    await adapter.close(session)


@pytest.mark.asyncio
async def test_repeating_active_model_skips_redundant_setters() -> None:
    adapter, proc = await _probed_adapter()
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(model="composer-2.5[fast=false]", mode="ask"),
            launch=_launch(),
        )
    )
    proc.requests.clear()
    await adapter.submit(
        session,
        TurnRequest(turn_id=uuid4(), prompt="again", model="composer-2.5[fast=false]"),
    )
    assert _setter_calls(proc) == []
    assert any(msg.get("method") == "session/prompt" for msg in proc.requests)
    await adapter.close(session)


@pytest.mark.asyncio
async def test_configuration_failure_does_not_begin_turn_or_prompt() -> None:
    adapter, proc = await _probed_adapter()
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(model="composer-2.5[fast=false]", mode="ask"),
            launch=_launch(),
        )
    )
    begin_calls: list[object] = []
    original_begin = adapter._normalizer.begin_turn  # pyright: ignore[reportPrivateUsage]

    def tracking_begin(turn_id: object) -> None:
        begin_calls.append(turn_id)
        original_begin(turn_id)  # type: ignore[arg-type]

    adapter._normalizer.begin_turn = tracking_begin  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
    proc.requests.clear()
    with pytest.raises(DomainError) as exc:
        await adapter.submit(
            session,
            TurnRequest(turn_id=uuid4(), prompt="nope", model="missing-model"),
        )
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
    assert begin_calls == []
    assert not any(msg.get("method") == "session/prompt" for msg in proc.requests)
    await adapter.close(session)


@pytest.mark.asyncio
async def test_failed_partial_override_is_restored_before_next_prompt() -> None:
    adapter, proc = await _probed_adapter()
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(model="composer-2.5[fast=false]", mode="ask"),
            launch=_launch(),
        )
    )
    proc.requests.clear()
    with pytest.raises(DomainError) as exc:
        await adapter.submit(
            session,
            TurnRequest(
                turn_id=uuid4(),
                prompt="nope",
                model="gpt-5.6-sol[reasoning=high,max=true]",
            ),
        )
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
    assert _setter_pairs(proc) == [("model", "gpt-5.6-sol"), ("reasoning", "high")]
    assert adapter._current_model_selection == _CursorModelSelection(  # pyright: ignore[reportPrivateUsage]
        "gpt-5.6-sol",
        (("context", "272k"), ("reasoning", "high"), ("fast", "false")),
    )

    proc.requests.clear()
    await adapter.submit(session, TurnRequest(turn_id=uuid4(), prompt="restored"))
    assert _setter_pairs(proc) == [("model", "composer-2.5")]
    assert adapter._current_model_selection == adapter._session_model_selection  # pyright: ignore[reportPrivateUsage]
    assert any(msg.get("method") == "session/prompt" for msg in proc.requests)
    await adapter.close(session)


@pytest.mark.asyncio
async def test_build_argv_ignores_model_and_mode() -> None:
    adapter = CursorAdapter()
    assert adapter.build_argv(_config(model="composer-2.5", mode="plan")) == ("acp",)


# ---------------------------------------------------------------------------
# Existing permission / close coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_protocol_failure_publishes_unknown_before_stream_close() -> None:
    adapter = CursorAdapter()
    turn_id = uuid4()
    adapter._normalizer.set_session("cursor-session")  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.begin_turn(turn_id)  # pyright: ignore[reportPrivateUsage]

    await adapter._emit_prompt_outcome_unknown_and_close(  # pyright: ignore[reportPrivateUsage]
        "connection closed"
    )

    event = await adapter._event_q.get()  # pyright: ignore[reportPrivateUsage]
    end = await adapter._event_q.get()  # pyright: ignore[reportPrivateUsage]
    assert isinstance(event, TurnOutcomeUnknownPayload)
    assert event.turn_id == turn_id
    assert event.delivery_phase == "delivered"
    assert end is None


@pytest.mark.asyncio
async def test_permission_request_and_answer_interaction() -> None:
    adapter = CursorAdapter()
    session = HarnessSession(
        conversation_id=uuid4(),
        binding_id=uuid4(),
        kind=HarnessKind.CURSOR,
        native_session_id="cursor-session",
    )
    adapter._session = session  # pyright: ignore[reportPrivateUsage]
    adapter._closed = False  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.set_session("cursor-session")  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.begin_turn(uuid4())  # pyright: ignore[reportPrivateUsage]
    responded: list[tuple[object, object]] = []

    async def respond(rpc_id: object, result: object) -> None:
        responded.append((rpc_id, result))

    adapter._connection = SimpleNamespace(respond=respond)  # type: ignore[assignment]

    await adapter._on_permission_request(  # pyright: ignore[reportPrivateUsage]
        SimpleNamespace(
            id="rpc-9",
            params={
                "sessionId": "cursor-session",
                "toolCall": {"toolCallId": "tool-1"},
                "options": [{"optionId": "allow-once", "kind": "allow_once"}],
            },
        )
    )
    event = adapter._event_q.get_nowait()  # pyright: ignore[reportPrivateUsage]
    assert isinstance(event, HarnessInteractionRequest)
    assert event.provider_correlation == {
        "json_rpc_request_id": "rpc-9",
        "tool_call_id": "tool-1",
        "native_session_id": "cursor-session",
    }
    interaction_id = event.payload.interaction_id

    await adapter.answer_interaction(
        session,
        InteractionAnswer(interaction_id=interaction_id, decision=ApprovalDecision.ALLOW_ONCE),
    )
    assert responded == [("rpc-9", {"outcome": {"outcome": "selected", "optionId": "allow-once"}})]
    assert interaction_id not in adapter._pending_interactions  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(DomainError) as exc:
        await adapter.answer_interaction(
            session,
            InteractionAnswer(interaction_id=uuid4(), decision=ApprovalDecision.ALLOW_ONCE),
        )
    assert exc.value.code is ErrorCode.INVALID_STATE


@pytest.mark.asyncio
async def test_close_cancels_pending_and_watch_prompt_branches() -> None:
    adapter = CursorAdapter()
    session = HarnessSession(
        conversation_id=uuid4(),
        binding_id=uuid4(),
        kind=HarnessKind.CURSOR,
        native_session_id="cursor-session",
    )
    adapter._session = session  # pyright: ignore[reportPrivateUsage]
    adapter._closed = False  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.set_session("cursor-session")  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.begin_turn(uuid4())  # pyright: ignore[reportPrivateUsage]

    notified: list[tuple[str, object]] = []
    responded: list[tuple[object, object]] = []

    async def respond(rpc_id: object, result: object) -> None:
        responded.append((rpc_id, result))

    async def notify(method: str, params: object) -> None:
        notified.append((method, params))

    async def close() -> None:
        return None

    adapter._connection = SimpleNamespace(  # type: ignore[assignment]
        respond=respond,
        notify=notify,
        close=close,
    )
    adapter._pending_interactions[uuid4()] = (  # pyright: ignore[reportPrivateUsage]
        "rpc-pending",
        [{"optionId": "allow-once", "kind": "allow_once"}],
    )

    # Unmapped decision rejected before native respond.
    await adapter._on_permission_request(  # pyright: ignore[reportPrivateUsage]
        SimpleNamespace(
            id="rpc-map",
            params={
                "sessionId": "cursor-session",
                "options": [{"optionId": "allow-once", "kind": "allow_once"}],
            },
        )
    )
    event = adapter._event_q.get_nowait()  # pyright: ignore[reportPrivateUsage]
    assert isinstance(event, HarnessInteractionRequest)
    with pytest.raises(DomainError) as unmapped:
        await adapter.answer_interaction(
            session,
            InteractionAnswer(
                interaction_id=event.payload.interaction_id,
                decision=ApprovalDecision.ALLOW_SESSION,
            ),
        )
    assert unmapped.value.code is ErrorCode.INVALID_STATE

    await adapter.interrupt(session)
    assert any(method == "session/cancel" for method, _ in notified)
    assert any(rpc_id == "rpc-pending" for rpc_id, _ in responded)

    # watch_prompt error branches
    adapter._closed = False  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.begin_turn(uuid4())  # pyright: ignore[reportPrivateUsage]
    remote = asyncio.get_running_loop().create_future()
    remote.set_exception(JsonRpcRemoteError(code=-1, message="remote boom"))
    await adapter._watch_prompt(remote)  # pyright: ignore[reportPrivateUsage]
    failed = adapter._event_q.get_nowait()  # pyright: ignore[reportPrivateUsage]
    assert isinstance(failed, TurnFailedPayload)

    adapter._normalizer.begin_turn(uuid4())  # pyright: ignore[reportPrivateUsage]
    generic = asyncio.get_running_loop().create_future()
    generic.set_exception(RuntimeError("explode"))
    await adapter._watch_prompt(generic)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(adapter._event_q.get_nowait(), TurnFailedPayload)  # pyright: ignore[reportPrivateUsage]

    adapter._normalizer.begin_turn(uuid4())  # pyright: ignore[reportPrivateUsage]
    ok = asyncio.get_running_loop().create_future()
    ok.set_result({"stopReason": "end_turn"})
    await adapter._watch_prompt(ok)  # pyright: ignore[reportPrivateUsage]

    await adapter.close(session)
    await adapter.close(session)  # idempotent
    with pytest.raises(DomainError):
        adapter._require_session(session)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_initialize_capability_decoded_from_fake_process_bytes() -> None:
    """Decode the actual initialize frame written to the fake process."""
    adapter, proc = await _probed_adapter()
    await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(model=None, mode=None),
            launch=_launch(),
        )
    )
    # The fake records decoded JSON; also assert the serialized shape matches.
    init = next(msg for msg in proc.requests if msg.get("method") == "initialize")
    assert json.loads(json.dumps(init["params"]))["clientCapabilities"] == {
        "_meta": {"parameterizedModelPicker": True}
    }
