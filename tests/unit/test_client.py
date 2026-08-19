"""Unit tests for the official async HTTP client."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from talktoharnesses.client import APIError, AsyncTalkToHarnessesClient
from talktoharnesses.domain import (
    ActivityProjection,
    ActivityStatus,
    ApprovalDecision,
    ApprovalRequestPayload,
    ApprovalRuleDecision,
    ApprovalRuleInput,
    ApprovalRuleProjection,
    CommandKind,
    CommandProjection,
    CommandStatus,
    Conversation,
    ConversationDetail,
    ConversationEvent,
    ConversationSearchHit,
    ConversationShell,
    ConversationSnapshot,
    ConversationStatus,
    ExactArgvMatcher,
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessKind,
    HarnessModeInfo,
    HarnessModelInfo,
    HarnessProbeProjection,
    HarnessProjection,
    InteractionAuditProjection,
    InteractionKind,
    InteractionProjection,
    InteractionStatus,
    MessageProjection,
    MessageRole,
    Page,
    PlanItem,
    PlanProjection,
    PrincipalGlobalRuleScope,
    ReadinessProjection,
    RetentionPolicyProjection,
    RetentionPreviewProjection,
    SearchSnippet,
    SubmitTurnResult,
    SyncProjection,
    TokenProjection,
    ToolOutcome,
    ToolProjection,
    TranscriptDocument,
    TranscriptMessage,
    TranscriptTurn,
    TurnProjection,
    TurnStatus,
)
from talktoharnesses.domain.events import ConversationMetadataChangedPayload

_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
_BASE = "http://testserver/api/v1/"
_HARNESS_ID = UUID("11111111-1111-1111-1111-111111111111")
_CONV_ID = UUID("22222222-2222-2222-2222-222222222222")
_TURN_ID = UUID("33333333-3333-3333-3333-333333333333")
_CMD_ID = UUID("44444444-4444-4444-4444-444444444444")
_RULE_ID = UUID("55555555-5555-5555-5555-555555555555")
_INTERACTION_ID = UUID("66666666-6666-6666-6666-666666666666")
_AUDIT_ID = UUID("77777777-7777-7777-7777-777777777777")
_MSG_ID = UUID("88888888-8888-8888-8888-888888888888")
_TOOL_ID = UUID("99999999-9999-9999-9999-999999999999")
_PLAN_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_ACTIVITY_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_EVENT_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


class RecordingHandler:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._handlers: list[Callable[[httpx.Request], httpx.Response]] = []

    def push(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handlers.append(handler)

    def respond(
        self,
        status: int,
        body: bytes | str | dict[str, Any] | list[Any] | BaseModel | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        if body is None:
            content = b""
        elif isinstance(body, bytes):
            content = body
        elif isinstance(body, str):
            content = body.encode()
        elif isinstance(body, BaseModel):
            content = body.model_dump_json().encode()
        else:
            content = json.dumps(body).encode()

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status,
                content=content,
                headers=headers or {"content-type": "application/json"},
                request=request,
            )

        self.push(_handler)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._handlers:
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        handler = self._handlers.pop(0)
        return handler(request)


@pytest.fixture
def handler() -> RecordingHandler:
    return RecordingHandler()


def _conversation() -> Conversation:
    return Conversation(
        id=_CONV_ID,
        owner_id="1",
        status=ConversationStatus.IDLE,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _snapshot(sequence: int = 1) -> ConversationSnapshot:
    return ConversationSnapshot(
        sequence=sequence,
        detail=ConversationDetail(conversation=_conversation()),
    )


def _command() -> CommandProjection:
    return CommandProjection(
        id=_CMD_ID,
        kind=CommandKind.SUBMIT_TURN,
        status=CommandStatus.ACCEPTED,
        target_turn_id=_TURN_ID,
        idempotency_key="k1",
        created_at=_NOW,
    )


def _turn() -> TurnProjection:
    return TurnProjection(
        id=_TURN_ID,
        conversation_id=_CONV_ID,
        status=TurnStatus.QUEUED,
        created_at=_NOW,
    )


def _harness() -> HarnessProjection:
    return HarnessProjection(
        id=_HARNESS_ID,
        owner_id="1",
        name="local",
        kind=HarnessKind.GROK,
        configuration=HarnessConfiguration(
            kind=HarnessKind.GROK,
            working_directory="/tmp/ws",
        ),
        created_at=_NOW,
    )


def _probe() -> HarnessProbeProjection:
    return HarnessProbeProjection(
        harness_id=_HARNESS_ID,
        capabilities=HarnessCapabilities(kind=HarnessKind.GROK, version="1.0.0"),
        probed_at=_NOW,
    )


def _rule() -> ApprovalRuleProjection:
    return ApprovalRuleProjection(
        id=_RULE_ID,
        principal_id="1",
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactArgvMatcher(argv=("ls",)),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _rule_input() -> ApprovalRuleInput:
    return ApprovalRuleInput(
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactArgvMatcher(argv=("ls",)),
    )


def _interaction() -> InteractionProjection:
    return InteractionProjection(
        id=_INTERACTION_ID,
        kind=InteractionKind.APPROVAL,
        status=InteractionStatus.PENDING,
        turn_id=_TURN_ID,
        request=ApprovalRequestPayload(available_decisions=(ApprovalDecision.ALLOW_ONCE,)),
        created_at=_NOW,
    )


def _audit() -> InteractionAuditProjection:
    return InteractionAuditProjection(
        id=_AUDIT_ID,
        principal_id="1",
        interaction_id=_INTERACTION_ID,
        conversation_id=_CONV_ID,
        turn_id=_TURN_ID,
        kind=InteractionKind.APPROVAL,
        created_at=_NOW,
    )


def _shell() -> ConversationShell:
    return ConversationShell(
        id=_CONV_ID,
        title="t",
        status=ConversationStatus.IDLE,
        updated_at=_NOW,
    )


def _transcript() -> TranscriptDocument:
    return TranscriptDocument(
        format="talktoharnesses.canonical-transcript",
        version=1,
        title="imported",
        turns=(
            TranscriptTurn(
                entries=(TranscriptMessage(role="user", text="hi"),),
            ),
        ),
    )


def _assert_auth(request: httpx.Request, token: str = "tok-a") -> None:
    assert request.headers["Authorization"] == f"Bearer {token}"


def _assert_url(request: httpx.Request, path: str) -> None:
    assert str(request.url).startswith(f"http://testserver/api/v1/{path}")


# ---------------------------------------------------------------------------
# Lifecycle / URL / token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://testserver/api/v1", "http://testserver/api/v1/"),
        ("http://testserver/api/v1/", "http://testserver/api/v1/"),
        ("https://host.example/api/v1/", "https://host.example/api/v1/"),
    ],
)
def test_base_url_normalization(base_url: str, expected: str) -> None:
    with patch("talktoharnesses.client.httpx.AsyncClient", wraps=httpx.AsyncClient) as mock_cls:
        client = AsyncTalkToHarnessesClient(base_url)
        assert client._base_url == expected  # type: ignore[attr-defined]
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["base_url"] == expected


def test_relative_base_url_rejected() -> None:
    with pytest.raises(ValueError, match="absolute"):
        AsyncTalkToHarnessesClient("/api/v1/")
    with pytest.raises(ValueError, match="absolute"):
        AsyncTalkToHarnessesClient("ftp://host/api/v1/")


@pytest.mark.asyncio
async def test_context_manager_and_aclose(handler: RecordingHandler) -> None:
    handler.respond(200, {"status": "ok"})
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    with patch("talktoharnesses.client.httpx.AsyncClient", side_effect=factory):
        async with AsyncTalkToHarnessesClient(_BASE) as client:
            result = await client.health()
            assert result == {"status": "ok"}
        # Explicit close after context is already closed should still be safe.
        await client.aclose()


@pytest.mark.asyncio
async def test_token_omitted_and_supplied(handler: RecordingHandler) -> None:
    handler.respond(200, {"status": "ok"})
    handler.respond(200, {"status": "ok"})
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    with patch("talktoharnesses.client.httpx.AsyncClient", side_effect=factory):
        async with AsyncTalkToHarnessesClient(_BASE) as bare:
            assert bare.token is None
            await bare.health()
            assert "Authorization" not in handler.requests[0].headers

        async with AsyncTalkToHarnessesClient(_BASE, token="secret") as authed:
            assert authed.token == "secret"
            await authed.health()
            assert handler.requests[1].headers["Authorization"] == "Bearer secret"


@pytest.mark.asyncio
async def test_per_request_timeout_override_and_inherit() -> None:
    recorded: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "ok"},
            headers={"content-type": "application/json"},
            request=request,
        )

    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        client = real_cls(*args, **kwargs)
        original_request = client.request

        async def request(method: str, url: httpx.URL | str, **kw: Any) -> httpx.Response:
            recorded.append(kw.get("timeout"))
            return await original_request(method, url, **kw)

        client.request = request  # type: ignore[method-assign]
        return client

    with patch("talktoharnesses.client.httpx.AsyncClient", side_effect=factory):
        async with AsyncTalkToHarnessesClient(_BASE, timeout=12.0) as client:
            await client.health()
            await client.health(timeout=None)
            await client.health(timeout=1.5)

    assert recorded == [12.0, None, 1.5]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_error_parsing(handler: RecordingHandler) -> None:
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    with patch("talktoharnesses.client.httpx.AsyncClient", side_effect=factory):
        async with AsyncTalkToHarnessesClient(_BASE, token="t") as client:
            handler.respond(401, {"code": "authentication_failed", "message": "auth failed"})
            with pytest.raises(APIError) as exc_info:
                await client.get_conversation(_CONV_ID)
            err = exc_info.value
            assert err.status_code == 401
            assert err.code == "authentication_failed"
            assert err.message == "auth failed"
            assert "401" in str(err)
            assert "authentication_failed" in str(err)

            handler.respond(409, {"code": "conversation_busy", "message": "conversation busy"})
            with pytest.raises(APIError) as conflict:
                await client.get_conversation(_CONV_ID)
            assert conflict.value.code == "conversation_busy"

            handler.respond(500, b"not-json")
            with pytest.raises(APIError) as generic:
                await client.get_conversation(_CONV_ID)
            assert generic.value.code is None
            assert generic.value.message == "HTTP request failed"

            handler.respond(200, _snapshot())  # wrong status for create (needs 201)
            with pytest.raises(APIError) as unexpected:
                await client.create_conversation(_HARNESS_ID)
            assert unexpected.value.status_code == 200

            handler.respond(200, b'{"not": "a snapshot"}')
            with pytest.raises(ValidationError):
                await client.get_conversation(_CONV_ID)


# ---------------------------------------------------------------------------
# System / auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_ready_and_tokens(handler: RecordingHandler) -> None:
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    with patch("talktoharnesses.client.httpx.AsyncClient", side_effect=factory):
        async with AsyncTalkToHarnessesClient(_BASE, token="old-token") as client:
            handler.respond(200, {"status": "ok"})
            assert await client.health() == {"status": "ok"}
            assert handler.requests[-1].method == "GET"
            _assert_url(handler.requests[-1], "health")

            handler.respond(503, ReadinessProjection(ready=False, reason="not_ready"))
            ready = await client.ready()
            assert ready.ready is False
            assert ready.reason == "not_ready"
            assert handler.requests[-1].method == "GET"

            new_token = TokenProjection(
                token="new-token",
                expires_at=_NOW,
            )
            handler.respond(200, new_token)
            rotated = await client.rotate_token()
            assert rotated.token == "new-token"
            assert client.token == "new-token"

            handler.respond(500, b"nope")
            with pytest.raises(APIError):
                await client.rotate_token()
            assert client.token == "new-token"

            handler.respond(200, b'{"token": 1}')  # invalid TokenProjection
            with pytest.raises(ValidationError):
                await client.rotate_token()
            assert client.token == "new-token"

            handler.respond(204)
            await client.revoke_token()
            assert client.token is None
            assert handler.requests[-1].method == "POST"
            _assert_url(handler.requests[-1], "auth/token/revoke")

            handler.respond(500, b"x")
            client._token = "still-here"  # type: ignore[attr-defined]
            with pytest.raises(APIError):
                await client.revoke_token()
            assert client.token == "still-here"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        b'{"status": 1}',
        b'["status", "ok"]',
        b"not-json",
    ],
)
async def test_health_rejects_malformed_success_payload(
    handler: RecordingHandler,
    payload: bytes,
) -> None:
    handler.respond(200, payload)
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    with patch("talktoharnesses.client.httpx.AsyncClient", side_effect=factory):
        async with AsyncTalkToHarnessesClient(_BASE) as client:
            with pytest.raises(ValidationError):
                await client.health()


# ---------------------------------------------------------------------------
# Harnesses / conversations / pages — representative coverage of every method
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harness_methods(handler: RecordingHandler) -> None:
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    config = HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp/ws")
    with patch("talktoharnesses.client.httpx.AsyncClient", side_effect=factory):
        async with AsyncTalkToHarnessesClient(_BASE, token="tok-a") as client:
            page = Page[HarnessProjection](items=(_harness(),), next_cursor="c1")
            handler.respond(200, page)
            listed = await client.list_harnesses(cursor=None, limit=10)
            assert listed.items[0].id == _HARNESS_ID
            req = handler.requests[-1]
            assert req.method == "GET"
            _assert_url(req, "harnesses")
            assert req.url.params["limit"] == "10"
            assert "cursor" not in req.url.params
            _assert_auth(req)

            handler.respond(200, Page[HarnessProjection](items=(), next_cursor=None))
            await client.list_harnesses(cursor="abc", limit=5)
            assert handler.requests[-1].url.params["cursor"] == "abc"

            handler.respond(201, _harness())
            created = await client.create_harness(name="local", configuration=config)
            assert created.name == "local"
            body = json.loads(handler.requests[-1].content)
            assert body == {
                "name": "local",
                "configuration": config.model_dump(mode="json", exclude_none=True),
            }
            assert body["configuration"]["yolo"] is False

            yolo_config = HarnessConfiguration(
                kind=HarnessKind.GROK,
                working_directory="/tmp/ws",
                yolo=True,
            )
            handler.respond(201, _harness())
            await client.create_harness(name="yolo", configuration=yolo_config)
            yolo_body = json.loads(handler.requests[-1].content)
            assert yolo_body["configuration"]["yolo"] is True
            assert handler.requests[-1].method == "POST"

            handler.respond(200, _harness())
            fetched = await client.get_harness(_HARNESS_ID)
            assert fetched.id == _HARNESS_ID
            assert handler.requests[-1].method == "GET"
            _assert_url(handler.requests[-1], f"harnesses/{_HARNESS_ID}")

            handler.respond(204)
            await client.delete_harness(_HARNESS_ID)
            assert handler.requests[-1].method == "DELETE"
            _assert_url(handler.requests[-1], f"harnesses/{_HARNESS_ID}")

            handler.respond(200, _probe())
            assert (await client.probe_harness(_HARNESS_ID)).harness_id == _HARNESS_ID
            assert handler.requests[-1].method == "POST"
            _assert_url(handler.requests[-1], f"harnesses/{_HARNESS_ID}/probe")

            handler.respond(200, _probe())
            assert (await client.get_harness_capabilities(_HARNESS_ID)).harness_id == _HARNESS_ID
            assert handler.requests[-1].method == "GET"

            models = (HarnessModelInfo(id="m1", label="M1"),)
            handler.respond(200, [m.model_dump(mode="json") for m in models])
            got_models = await client.get_harness_models(_HARNESS_ID)
            assert got_models[0].id == "m1"

            modes = (HarnessModeInfo(id="agent"),)
            handler.respond(200, [m.model_dump(mode="json") for m in modes])
            got_modes = await client.get_harness_modes(_HARNESS_ID)
            assert got_modes[0].id == "agent"


@pytest.mark.asyncio
async def test_conversation_methods(handler: RecordingHandler) -> None:
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    snap = _snapshot()
    with patch("talktoharnesses.client.httpx.AsyncClient", side_effect=factory):
        async with AsyncTalkToHarnessesClient(_BASE, token="tok-a") as client:
            shell_page = Page[ConversationShell](items=(_shell(),), next_cursor=None)
            handler.respond(200, shell_page)
            listed = await client.list_conversations(include_archived=False, limit=20)
            assert listed.items[0].id == _CONV_ID
            params = handler.requests[-1].url.params
            assert params["include_archived"] == "false"
            assert params["limit"] == "20"

            handler.respond(201, snap)
            created = await client.create_conversation(_HARNESS_ID, title="T")
            assert created.detail.conversation.id == _CONV_ID
            assert json.loads(handler.requests[-1].content) == {
                "harness_id": str(_HARNESS_ID),
                "title": "T",
            }

            handler.respond(201, snap)
            await client.create_conversation(_HARNESS_ID)
            assert json.loads(handler.requests[-1].content) == {
                "harness_id": str(_HARNESS_ID),
            }

            doc = _transcript()
            handler.respond(201, snap)
            imported = await client.import_transcript(_HARNESS_ID, doc)
            assert imported.sequence == 1
            payload = json.loads(handler.requests[-1].content)
            assert payload["harness_id"] == str(_HARNESS_ID)
            assert payload["document"]["format"] == "talktoharnesses.canonical-transcript"

            hit = ConversationSearchHit(
                conversation=_shell(),
                snippet=SearchSnippet(text="hi"),
            )
            handler.respond(200, Page[ConversationSearchHit](items=(hit,)))
            search = await client.search_conversations("hi", limit=5)
            assert search.items[0].snippet is not None
            assert handler.requests[-1].url.params["q"] == "hi"

            for method_name, path_suffix, http_method in [
                ("get_conversation", "", "GET"),
                ("archive_conversation", "/archive", "POST"),
                ("unarchive_conversation", "/unarchive", "POST"),
                ("pin_conversation", "/pin", "POST"),
                ("unpin_conversation", "/unpin", "POST"),
                ("unsnooze_conversation", "/unsnooze", "POST"),
            ]:
                handler.respond(200, snap)
                result = await getattr(client, method_name)(_CONV_ID)
                assert isinstance(result, ConversationSnapshot)
                req = handler.requests[-1]
                assert req.method == http_method
                expected = f"conversations/{_CONV_ID}{path_suffix}"
                _assert_url(req, expected)

            handler.respond(200, snap)
            until = datetime(2026, 9, 1, tzinfo=UTC)
            await client.snooze_conversation(_CONV_ID, until=until)
            assert json.loads(handler.requests[-1].content) == {"until": until.isoformat()}

            handler.respond(200, snap)
            await client.set_retention_exemption(_CONV_ID, exempt=True)
            assert json.loads(handler.requests[-1].content) == {"exempt": True}

            handler.respond(204)
            await client.delete_conversation(_CONV_ID)
            assert handler.requests[-1].method == "DELETE"

            handler.respond(200, doc)
            exported = await client.export_transcript(_CONV_ID)
            assert exported.title == "imported"


@pytest.mark.asyncio
async def test_retention_and_history_pages(handler: RecordingHandler) -> None:
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    with patch("talktoharnesses.client.httpx.AsyncClient", side_effect=factory):
        async with AsyncTalkToHarnessesClient(_BASE, token="tok-a") as client:
            policy = RetentionPolicyProjection(months=12, updated_at=_NOW)
            handler.respond(200, policy)
            assert (await client.get_retention_policy()).months == 12

            handler.respond(200, policy)
            await client.replace_retention_policy(6)
            assert json.loads(handler.requests[-1].content) == {"months": 6}
            assert handler.requests[-1].method == "PUT"

            preview = RetentionPreviewProjection(
                cutoff=_NOW,
                soft_deleted_conversations=0,
                history_conversations=0,
                terminal_turns=0,
                waiting_turns=0,
            )
            handler.respond(200, preview)
            assert (await client.preview_retention()).cutoff == _NOW

            turn_page = Page[TurnProjection](items=(_turn(),))
            handler.respond(200, turn_page)
            assert (await client.page_turns(_CONV_ID, limit=3)).items[0].id == _TURN_ID
            assert handler.requests[-1].url.params["limit"] == "3"
            assert "cursor" not in handler.requests[-1].url.params

            msg_page = Page[MessageProjection](
                items=(
                    MessageProjection(
                        id=_MSG_ID,
                        turn_id=_TURN_ID,
                        role=MessageRole.USER,
                        text="hi",
                        created_at=_NOW,
                    ),
                )
            )
            handler.respond(200, msg_page)
            assert (await client.page_messages(_CONV_ID)).items[0].id == _MSG_ID

            tool_page = Page[ToolProjection](
                items=(
                    ToolProjection(
                        id=_TOOL_ID,
                        turn_id=_TURN_ID,
                        tool_name="bash",
                        outcome=ToolOutcome.SUCCESS,
                    ),
                )
            )
            handler.respond(200, tool_page)
            assert (await client.page_tools(_CONV_ID)).items[0].tool_name == "bash"

            plan_page = Page[PlanProjection](
                items=(
                    PlanProjection(
                        id=_PLAN_ID,
                        turn_id=_TURN_ID,
                        items=(PlanItem(id="1", title="step"),),
                    ),
                )
            )
            handler.respond(200, plan_page)
            assert (await client.page_plans(_CONV_ID)).items[0].id == _PLAN_ID

            activity_page = Page[ActivityProjection](
                items=(
                    ActivityProjection(
                        id=_ACTIVITY_ID,
                        conversation_id=_CONV_ID,
                        parent_turn_id=_TURN_ID,
                        status=ActivityStatus.RUNNING,
                        created_at=_NOW,
                    ),
                )
            )
            handler.respond(200, activity_page)
            assert (await client.page_activity(_CONV_ID)).items[0].id == _ACTIVITY_ID


@pytest.mark.asyncio
async def test_turn_control_and_interactions(handler: RecordingHandler) -> None:
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    with patch("talktoharnesses.client.httpx.AsyncClient", side_effect=factory):
        async with AsyncTalkToHarnessesClient(_BASE, token="tok-a") as client:
            result = SubmitTurnResult(command=_command(), turn=_turn())
            handler.respond(202, result)
            submitted = await client.submit_turn(
                _CONV_ID,
                prompt="hello",
                idempotency_key="idem-1",
                model="m1",
            )
            assert submitted.turn.id == _TURN_ID
            req = handler.requests[-1]
            assert req.method == "POST"
            assert req.headers["Idempotency-Key"] == "idem-1"
            assert json.loads(req.content) == {"prompt": "hello", "model": "m1"}

            handler.respond(202, result)
            await client.submit_turn(_CONV_ID, prompt="x", idempotency_key="k")
            assert json.loads(handler.requests[-1].content) == {"prompt": "x"}

            handler.respond(200, _snapshot())
            await client.edit_queued_prompt(_CONV_ID, prompt="edited")
            assert handler.requests[-1].method == "PATCH"
            assert json.loads(handler.requests[-1].content) == {"prompt": "edited"}

            handler.respond(200, _command())
            cmd = await client.cancel_queued_prompt(_CONV_ID)
            assert cmd is not None and cmd.id == _CMD_ID

            handler.respond(204)
            assert await client.cancel_queued_prompt(_CONV_ID) is None

            handler.respond(202, _command())
            await client.steer(_CONV_ID, prompt="nudge", idempotency_key="s1")
            assert handler.requests[-1].headers["Idempotency-Key"] == "s1"
            assert json.loads(handler.requests[-1].content) == {"prompt": "nudge"}

            handler.respond(202, _command())
            await client.switch_harness(
                _CONV_ID,
                harness_id=_HARNESS_ID,
                idempotency_key="sw1",
            )
            assert json.loads(handler.requests[-1].content) == {
                "harness_id": str(_HARNESS_ID),
            }
            assert handler.requests[-1].headers["Idempotency-Key"] == "sw1"

            handler.respond(202, _command())
            await client.interrupt(_CONV_ID)
            assert handler.requests[-1].method == "POST"
            assert "Idempotency-Key" not in handler.requests[-1].headers
            assert handler.requests[-1].content in (b"", b"null")

            handler.respond(200, Page[InteractionProjection](items=(_interaction(),)))
            interactions = await client.list_interactions(_CONV_ID)
            assert interactions.items[0].id == _INTERACTION_ID

            handler.respond(200, _interaction())
            await client.update_interaction_draft(
                _CONV_ID,
                _INTERACTION_ID,
                draft={"a": 1},
            )
            assert json.loads(handler.requests[-1].content) == {"draft": {"a": 1}}

            handler.respond(202, _command())
            await client.resolve_interaction(
                _CONV_ID,
                _INTERACTION_ID,
                decision=ApprovalDecision.ALLOW_ONCE,
                create_rule=_rule_input(),
            )
            body = json.loads(handler.requests[-1].content)
            assert body["decision"] == "allow_once"
            assert body["create_rule"]["decision"] == "allow"
            assert "answers" not in body

            handler.respond(202, _command())
            await client.resolve_interaction(
                _CONV_ID,
                _INTERACTION_ID,
                answers={"choice": "a"},
            )
            assert json.loads(handler.requests[-1].content) == {"answers": {"choice": "a"}}


@pytest.mark.asyncio
async def test_approval_rules_and_audits(handler: RecordingHandler) -> None:
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    with patch("talktoharnesses.client.httpx.AsyncClient", side_effect=factory):
        async with AsyncTalkToHarnessesClient(_BASE, token="tok-a") as client:
            handler.respond(200, Page[ApprovalRuleProjection](items=(_rule(),)))
            rules = await client.list_approval_rules(limit=2)
            assert rules.items[0].id == _RULE_ID

            handler.respond(201, _rule())
            created = await client.create_approval_rule(_rule_input())
            assert created.id == _RULE_ID
            assert json.loads(handler.requests[-1].content) == _rule_input().model_dump(mode="json")

            handler.respond(200, _rule())
            assert (await client.get_approval_rule(_RULE_ID)).id == _RULE_ID

            handler.respond(200, _rule())
            await client.replace_approval_rule(_RULE_ID, _rule_input())
            assert handler.requests[-1].method == "PUT"

            handler.respond(204)
            await client.delete_approval_rule(_RULE_ID)
            assert handler.requests[-1].method == "DELETE"

            handler.respond(200, Page[InteractionAuditProjection](items=(_audit(),)))
            audits = await client.list_interaction_audits()
            assert audits.items[0].id == _AUDIT_ID

            handler.respond(200, _audit())
            assert (await client.get_interaction_audit(_AUDIT_ID)).id == _AUDIT_ID


# ---------------------------------------------------------------------------
# SSE streaming
# ---------------------------------------------------------------------------


class _ByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


def _sse_response(
    request: httpx.Request,
    chunks: list[bytes],
    *,
    status: int = 200,
    content_type: str = "text/event-stream",
) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": content_type},
        stream=_ByteStream(chunks),
        request=request,
    )


def _frame(*, event: str, data: str, event_id: int) -> bytes:
    parts = [f"id: {event_id}", f"event: {event}"]
    for line in data.split("\n"):
        parts.append(f"data: {line}")
    parts.append("")
    return ("\n".join(parts) + "\n").encode()


def _event_payload(sequence: int = 3) -> ConversationEvent:
    return ConversationEvent(
        event_id=_EVENT_ID,
        conversation_id=_CONV_ID,
        sequence=sequence,
        timestamp=_NOW,
        type="conversation_metadata_changed",
        payload=ConversationMetadataChangedPayload(),
    )


def _deletion_event(sequence: int = 9) -> ConversationEvent:
    return ConversationEvent(
        event_id=uuid4(),
        conversation_id=_CONV_ID,
        sequence=sequence,
        timestamp=_NOW,
        type="conversation_metadata_changed",
        payload=ConversationMetadataChangedPayload(deleted_at=_NOW),
    )


@pytest.mark.asyncio
async def test_sse_parses_event_snapshot_sync_and_fragments() -> None:
    event = _event_payload(3)
    snap = _snapshot(sequence=1)
    sync = SyncProjection(sequence=1)
    frames = (
        b": keepalive\n\n"
        + _frame(event=event.type, data=event.model_dump_json(), event_id=3)
        + _frame(event="snapshot", data=snap.model_dump_json(), event_id=1)
        + _frame(event="sync", data=sync.model_dump_json(), event_id=1)
    )
    # Fragment across chunks including CRLF and multiline data.
    multi = (
        b"id: 4\r\nevent: conversation_metadata_changed\r\n"
        + b"data: "
        + event.model_copy(update={"sequence": 4}).model_dump_json().encode()[:20]
    )
    rest = event.model_copy(update={"sequence": 4}).model_dump_json().encode()[20:] + b"\r\n\r\n"

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            assert request.headers.get("Last-Event-ID") is None
            # Split UTF-8 and frames
            return _sse_response(
                request,
                [frames[:15], frames[15:], multi, rest],
            )
        return _sse_response(request, [b""])

    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    with patch("talktoharnesses.client.httpx.AsyncClient", side_effect=factory):
        async with AsyncTalkToHarnessesClient(_BASE, token="tok-a") as client:
            items: list[Any] = []
            async for item in client.stream_conversation_events(_CONV_ID):
                items.append(item)
                if len(items) >= 4:
                    break
            assert isinstance(items[0], ConversationEvent)
            assert isinstance(items[1], ConversationSnapshot)
            assert isinstance(items[2], SyncProjection)
            assert items[1].sequence == items[2].sequence == 1
            assert isinstance(items[3], ConversationEvent)
            assert items[3].sequence == 4


@pytest.mark.asyncio
async def test_sse_reconnect_sends_zero_last_event_id() -> None:
    """Reconnect after empty stream still sends Last-Event-ID: 0."""
    seen: list[str | None] = []
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        seen.append(request.headers.get("Last-Event-ID"))
        if state["n"] == 1:
            return _sse_response(request, [b""])
        return _sse_response(
            request,
            [
                _frame(
                    event="conversation_metadata_changed",
                    data=_deletion_event(1).model_dump_json(),
                    event_id=1,
                )
            ],
        )

    async def fake_sleep(delay: float) -> None:
        return None

    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    with (
        patch("talktoharnesses.client.httpx.AsyncClient", side_effect=factory),
        patch("talktoharnesses.client.asyncio.sleep", side_effect=fake_sleep),
    ):
        async with AsyncTalkToHarnessesClient(_BASE, token="tok-a") as client:
            items = [item async for item in client.stream_conversation_events(_CONV_ID)]

    assert seen[0] is None
    assert seen[1] == "0"
    assert len(items) == 1


@pytest.mark.asyncio
async def test_sse_after_sequence_and_reconnect_headers() -> None:
    event = _event_payload(5)
    seen: list[str | None] = []
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        seen.append(request.headers.get("Last-Event-ID"))
        if state["n"] == 1:
            # Clean EOF after one event
            return _sse_response(
                request,
                [_frame(event=event.type, data=event.model_dump_json(), event_id=5)],
            )
        if state["n"] == 2:
            return _sse_response(
                request,
                [
                    _frame(
                        event="conversation_metadata_changed",
                        data=_deletion_event(6).model_dump_json(),
                        event_id=6,
                    )
                ],
            )
        raise AssertionError("unexpected reconnect")

    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    with (
        patch("talktoharnesses.client.httpx.AsyncClient", side_effect=factory),
        patch("talktoharnesses.client.asyncio.sleep", side_effect=fake_sleep),
    ):
        async with AsyncTalkToHarnessesClient(_BASE, token="tok-a") as client:
            items = [
                item async for item in client.stream_conversation_events(_CONV_ID, after_sequence=2)
            ]
    assert seen[0] == "2"
    assert seen[1] == "5"
    assert len(items) == 2
    assert sleeps == [1.0]


@pytest.mark.asyncio
async def test_sse_transport_error_reconnect_and_backoff() -> None:
    event = _event_payload(1)
    state = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] <= 6:
            raise httpx.ConnectError("boom", request=request)
        return _sse_response(
            request,
            [
                _frame(event=event.type, data=event.model_dump_json(), event_id=1),
                _frame(
                    event="conversation_metadata_changed",
                    data=_deletion_event(2).model_dump_json(),
                    event_id=2,
                ),
            ],
        )

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    with (
        patch("talktoharnesses.client.httpx.AsyncClient", side_effect=factory),
        patch("talktoharnesses.client.asyncio.sleep", side_effect=fake_sleep),
    ):
        async with AsyncTalkToHarnessesClient(_BASE, token="tok-a") as client:
            items = [item async for item in client.stream_conversation_events(_CONV_ID)]

    assert [item.sequence for item in items] == [1, 2]
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]


@pytest.mark.asyncio
async def test_sse_backoff_reset_after_item() -> None:
    event = _event_payload(1)
    state = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] in (1, 2):
            raise httpx.ReadError("mid", request=request)
        if state["n"] == 3:
            # Successful item then clean EOF so the client reconnects.
            return _sse_response(
                request,
                [_frame(event=event.type, data=event.model_dump_json(), event_id=1)],
            )
        if state["n"] == 4:
            raise httpx.ReadError("again", request=request)
        return _sse_response(
            request,
            [
                _frame(
                    event="conversation_metadata_changed",
                    data=_deletion_event(2).model_dump_json(),
                    event_id=2,
                )
            ],
        )

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    with (
        patch("talktoharnesses.client.httpx.AsyncClient", side_effect=factory),
        patch("talktoharnesses.client.asyncio.sleep", side_effect=fake_sleep),
    ):
        async with AsyncTalkToHarnessesClient(_BASE, token="tok-a") as client:
            items = [item async for item in client.stream_conversation_events(_CONV_ID)]

    assert [i.sequence for i in items] == [1, 2]
    # Failures escalate 1 then 2; after a valid item the next reconnect sleeps 1
    # again instead of continuing at 4.
    assert sleeps == [1.0, 2.0, 1.0, 2.0]


@pytest.mark.asyncio
async def test_sse_timeout_none_and_midstream_transport_error() -> None:
    event = _event_payload(1)
    state = {"n": 0}
    sleeps: list[float] = []

    class FailingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield _frame(event=event.type, data=event.model_dump_json(), event_id=1)
            raise httpx.ReadError("mid-stream")

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=FailingStream(),
                request=request,
            )
        return _sse_response(
            request,
            [
                _frame(
                    event="conversation_metadata_changed",
                    data=_deletion_event(2).model_dump_json(),
                    event_id=2,
                )
            ],
        )

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    with (
        patch("talktoharnesses.client.httpx.AsyncClient", side_effect=factory),
        patch("talktoharnesses.client.asyncio.sleep", side_effect=fake_sleep),
    ):
        async with AsyncTalkToHarnessesClient(_BASE, token="tok-a", timeout=None) as client:
            items = [item async for item in client.stream_conversation_events(_CONV_ID)]

    assert [item.sequence for item in items] == [1, 2]
    assert sleeps == [1.0]


@pytest.mark.asyncio
async def test_sse_per_request_timeout_override() -> None:
    recorded: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            request,
            [
                _frame(
                    event="conversation_metadata_changed",
                    data=_deletion_event(1).model_dump_json(),
                    event_id=1,
                )
            ],
        )

    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        client = real_cls(*args, **kwargs)
        original_stream = client.stream

        def stream(method: str, url: httpx.URL | str, **kw: Any) -> Any:
            recorded.append(kw.get("timeout"))
            return original_stream(method, url, **kw)

        client.stream = stream  # type: ignore[method-assign]
        return client

    with patch("talktoharnesses.client.httpx.AsyncClient", side_effect=factory):
        async with AsyncTalkToHarnessesClient(_BASE, token="tok-a", timeout=12.0) as client:
            async for _item in client.stream_conversation_events(_CONV_ID):
                pass
            async for _item in client.stream_conversation_events(_CONV_ID, timeout=None):
                pass
            async for _item in client.stream_conversation_events(_CONV_ID, timeout=2.0):
                pass

    assert len(recorded) == 3
    inherited, disabled, override = recorded
    assert isinstance(inherited, httpx.Timeout)
    assert inherited.connect == 12.0
    assert inherited.write == 12.0
    assert inherited.pool == 12.0
    assert inherited.read is None
    assert isinstance(disabled, httpx.Timeout)
    assert disabled.connect is None
    assert disabled.write is None
    assert disabled.pool is None
    assert disabled.read is None
    assert isinstance(override, httpx.Timeout)
    assert override.connect == 2.0
    assert override.write == 2.0
    assert override.pool == 2.0
    assert override.read is None


@pytest.mark.asyncio
async def test_sse_decoder_edges() -> None:
    from talktoharnesses._sse import SseDecoder

    decoder = SseDecoder()
    assert decoder.feed(b"") == []
    assert decoder.feed(b"data: hi\n\n\n")  # blank lines inside are fine via split
    assert decoder.feed(b": only-comment\n\n") == []
    assert decoder.feed(b"\n\n") == []

    limited = SseDecoder(max_partial_bytes=8)
    with pytest.raises(ValueError, match="max_partial_bytes"):
        limited.feed(b"x" * 20)

    bad_utf = SseDecoder()
    with pytest.raises(ValueError, match="UTF-8"):
        bad_utf.feed(b"\xff\xff\n\n")

    unknown = SseDecoder()
    events = unknown.feed(b"retry: 10\nevent: ping\ndata: x\nid: 1\n\n")
    assert len(events) == 1
    assert events[0].event == "ping"
    assert events[0].id == "1"

    # Prefer CRLF terminator so an interior LF blank line is part of the block.
    blank_interior = SseDecoder()
    events = blank_interior.feed(b"event: x\n\ndata: y\r\n\r\n")
    assert len(events) == 1
    assert events[0].event == "x"
    assert events[0].data == "y"

    # data: without the optional space after the colon.
    nospace = SseDecoder()
    assert nospace.feed(b"data:plain\n\n")[0].data == "plain"

    unlimited = SseDecoder(max_partial_bytes=None)
    # Larger than the OpenCode default 1 MiB cap; allowed when cap is disabled.
    payload = b"y" * (1_048_576 + 64)
    assert unlimited.feed(b"data: " + payload) == []
    assert unlimited.feed(b"\n\n")[0].data == payload.decode()


@pytest.mark.asyncio
async def test_sse_terminal_deletion_no_reconnect() -> None:
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] > 1:
            raise AssertionError("should not reconnect after deletion")
        return _sse_response(
            request,
            [
                _frame(
                    event="conversation_metadata_changed",
                    data=_deletion_event(1).model_dump_json(),
                    event_id=1,
                )
            ],
        )

    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    with patch("talktoharnesses.client.httpx.AsyncClient", side_effect=factory):
        async with AsyncTalkToHarnessesClient(_BASE, token="tok-a") as client:
            items = [item async for item in client.stream_conversation_events(_CONV_ID)]
    assert len(items) == 1
    assert state["n"] == 1


@pytest.mark.asyncio
async def test_sse_error_paths_without_retry() -> None:
    transport_holder: dict[str, httpx.MockTransport] = {}

    def make_client(
        handler: Callable[[httpx.Request], httpx.Response],
    ) -> Any:
        transport_holder["t"] = httpx.MockTransport(handler)
        real_cls = httpx.AsyncClient

        def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = transport_holder["t"]
            return real_cls(*args, **kwargs)

        return patch("talktoharnesses.client.httpx.AsyncClient", side_effect=factory)

    # Non-200
    def bad_status(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            content=b'{"code":"authentication_failed","message":"auth failed"}',
            headers={"content-type": "application/json"},
            request=request,
        )

    with make_client(bad_status):
        async with AsyncTalkToHarnessesClient(_BASE, token="t") as client:
            with pytest.raises(APIError) as err:
                async for _ in client.stream_conversation_events(_CONV_ID):
                    pass
            assert err.value.status_code == 401

    # Invalid content type
    def bad_ct(request: httpx.Request) -> httpx.Response:
        return _sse_response(request, [b"data: x\n\n"], content_type="application/json")

    with make_client(bad_ct):
        async with AsyncTalkToHarnessesClient(_BASE, token="t") as client:
            with pytest.raises(ValueError, match="text/event-stream"):
                async for _ in client.stream_conversation_events(_CONV_ID):
                    pass

    # Missing id
    def missing_id(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            request,
            [b'event: sync\ndata: {"sequence":1}\n\n'],
        )

    with make_client(missing_id):
        async with AsyncTalkToHarnessesClient(_BASE, token="t") as client:
            with pytest.raises(ValueError, match="missing"):
                async for _ in client.stream_conversation_events(_CONV_ID):
                    pass

    # Non-integer id
    def bad_id(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            request,
            [b'id: no\nevent: sync\ndata: {"sequence":1}\n\n'],
        )

    with make_client(bad_id):
        async with AsyncTalkToHarnessesClient(_BASE, token="t") as client:
            with pytest.raises(ValueError, match="integer"):
                async for _ in client.stream_conversation_events(_CONV_ID):
                    pass

    # Sequence mismatch
    def seq_mismatch(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            request,
            [_frame(event="sync", data=SyncProjection(sequence=2).model_dump_json(), event_id=1)],
        )

    with make_client(seq_mismatch):
        async with AsyncTalkToHarnessesClient(_BASE, token="t") as client:
            with pytest.raises(ValueError, match="does not match payload sequence"):
                async for _ in client.stream_conversation_events(_CONV_ID):
                    pass

    # Event name mismatch
    event = _event_payload(1)

    def name_mismatch(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            request,
            [_frame(event="turn_started", data=event.model_dump_json(), event_id=1)],
        )

    with make_client(name_mismatch):
        async with AsyncTalkToHarnessesClient(_BASE, token="t") as client:
            with pytest.raises(ValueError, match="does not match ConversationEvent.type"):
                async for _ in client.stream_conversation_events(_CONV_ID):
                    pass

    # Invalid JSON
    def bad_json(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            request,
            [b"id: 1\nevent: sync\ndata: {not-json\n\n"],
        )

    with make_client(bad_json):
        async with AsyncTalkToHarnessesClient(_BASE, token="t") as client:
            with pytest.raises(ValidationError):
                async for _ in client.stream_conversation_events(_CONV_ID):
                    pass


@pytest.mark.asyncio
async def test_sse_cancellation_closes_response() -> None:
    closed = {"value": False}

    class TrackingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b": keepalive\n\n"
            await asyncio.sleep(3600)

        async def aclose(self) -> None:
            closed["value"] = True

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=TrackingStream(),
            request=request,
        )

    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    with patch("talktoharnesses.client.httpx.AsyncClient", side_effect=factory):
        async with AsyncTalkToHarnessesClient(_BASE, token="tok-a") as client:
            agen = cast(
                AsyncGenerator[Any, None],
                client.stream_conversation_events(_CONV_ID),
            )
            task = asyncio.create_task(agen.__anext__())
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await agen.aclose()

    assert closed["value"] is True


@pytest.mark.asyncio
async def test_sse_cancellation_during_backoff() -> None:
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        raise httpx.ConnectError("nope", request=request)

    started = asyncio.Event()
    real_sleep = asyncio.sleep

    async def slow_sleep(delay: float) -> None:
        started.set()
        await real_sleep(3600)

    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    with (
        patch("talktoharnesses.client.httpx.AsyncClient", side_effect=factory),
        patch("talktoharnesses.client.asyncio.sleep", side_effect=slow_sleep),
    ):
        async with AsyncTalkToHarnessesClient(_BASE, token="tok-a") as client:
            agen = cast(
                AsyncGenerator[Any, None],
                client.stream_conversation_events(_CONV_ID),
            )
            task = asyncio.create_task(agen.__anext__())
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await agen.aclose()
