"""Thin Ninja route handlers over TalkToHarnessesService."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from django.db import connection
from django.http import HttpRequest, StreamingHttpResponse
from ninja import Router

from talktoharnesses.django.api.schemas import (
    CreateConversationBody,
    CreateHarnessBody,
    EditQueuedPromptBody,
    InteractionDraftBody,
    ResolveInteractionBody,
    SnoozeBody,
    SteerBody,
    SubmitTurnBody,
)
from talktoharnesses.django.api.sse import iter_sse, parse_last_event_id
from talktoharnesses.django.asgi import get_service
from talktoharnesses.django.auth import owner_id_for_user, revoke_token, rotate_token
from talktoharnesses.domain.models import (
    ActivityProjection,
    CommandProjection,
    ConversationShell,
    ConversationSnapshot,
    ErrorProjection,
    HarnessModeInfo,
    HarnessModelInfo,
    HarnessProbeProjection,
    HarnessProjection,
    InteractionProjection,
    MessageProjection,
    Page,
    PlanProjection,
    SubmitTurnResult,
    TokenProjection,
    ToolProjection,
    TurnProjection,
)

router = Router()


def _owner(request: HttpRequest) -> str:
    return owner_id_for_user(cast(Any, request).auth)


def _auth_header(request: HttpRequest) -> str | None:
    return request.headers.get("Authorization")


# ---------------------------------------------------------------------------
# Public health / readiness
# ---------------------------------------------------------------------------


@router.get("/health", auth=None, response={200: dict[str, str]})
async def health(request: HttpRequest) -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", auth=None, response={200: dict[str, object], 503: ErrorProjection})
async def ready(request: HttpRequest) -> tuple[int, dict[str, object] | ErrorProjection]:
    from asgiref.sync import sync_to_async

    def _db_ok() -> bool:
        try:
            connection.ensure_connection()
            return bool(connection.is_usable())
        except Exception:
            return False

    db_ok = await sync_to_async(_db_ok, thread_sensitive=True)()
    try:
        service = get_service()
        service_ok = service.started
        # Publisher is always injected; readiness is service-started + DB.
        _ = service.publisher
        broker_ok = True
    except Exception:
        service_ok = False
        broker_ok = False
    if db_ok and service_ok and broker_ok:
        return 200, {"status": "ready", "database": True, "service": True, "broker": True}
    return 503, ErrorProjection(code="not_ready", message="service not ready")


# ---------------------------------------------------------------------------
# Auth token lifecycle
# ---------------------------------------------------------------------------


@router.post("/auth/token/rotate", response={200: TokenProjection})
async def rotate_auth_token(request: HttpRequest) -> TokenProjection:
    return await rotate_token(_auth_header(request))


@router.post("/auth/token/revoke", response={204: None})
async def revoke_auth_token(request: HttpRequest) -> tuple[int, None]:
    await revoke_token(_auth_header(request))
    return 204, None


# ---------------------------------------------------------------------------
# Harnesses
# ---------------------------------------------------------------------------


@router.get("/harnesses", response=Page[HarnessProjection])
async def list_harnesses(
    request: HttpRequest,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[HarnessProjection]:
    return await get_service().list_harnesses(_owner(request), cursor=cursor, limit=limit)


@router.post("/harnesses", response={201: HarnessProjection})
async def create_harness(
    request: HttpRequest, body: CreateHarnessBody
) -> tuple[int, HarnessProjection]:
    result = await get_service().create_harness(
        _owner(request),
        name=body.name,
        configuration=body.configuration.to_domain(),
    )
    return 201, result


@router.post("/harnesses/{harness_id}/probe", response={200: HarnessProbeProjection})
async def probe_harness(request: HttpRequest, harness_id: UUID) -> HarnessProbeProjection:
    return await get_service().probe_harness(_owner(request), harness_id)


@router.get("/harnesses/{harness_id}/capabilities", response=HarnessProbeProjection)
async def harness_capabilities(request: HttpRequest, harness_id: UUID) -> HarnessProbeProjection:
    return await get_service().get_harness_capabilities(_owner(request), harness_id)


@router.get("/harnesses/{harness_id}/models", response=list[HarnessModelInfo])
async def harness_models(request: HttpRequest, harness_id: UUID) -> list[HarnessModelInfo]:
    models = await get_service().get_harness_models(_owner(request), harness_id)
    return list(models)


@router.get("/harnesses/{harness_id}/modes", response=list[HarnessModeInfo])
async def harness_modes(request: HttpRequest, harness_id: UUID) -> list[HarnessModeInfo]:
    modes = await get_service().get_harness_modes(_owner(request), harness_id)
    return list(modes)


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


@router.get("/conversations", response=Page[ConversationShell])
async def list_conversations(
    request: HttpRequest,
    cursor: str | None = None,
    limit: int = 50,
    include_archived: bool = True,
) -> Page[ConversationShell]:
    return await get_service().list_conversations(
        _owner(request),
        cursor=cursor,
        limit=limit,
        include_archived=include_archived,
    )


@router.post("/conversations", response={201: ConversationSnapshot})
async def create_conversation(
    request: HttpRequest, body: CreateConversationBody
) -> tuple[int, ConversationSnapshot]:
    snap = await get_service().create_conversation(
        _owner(request),
        body.harness_id,
        title=body.title,
    )
    return 201, snap


@router.get("/conversations/search", response=Page[ConversationShell])
async def search_conversations(
    request: HttpRequest,
    q: str,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[ConversationShell]:
    return await get_service().search_conversations(_owner(request), q, cursor=cursor, limit=limit)


@router.get("/conversations/{conversation_id}", response=ConversationSnapshot)
async def get_conversation(request: HttpRequest, conversation_id: UUID) -> ConversationSnapshot:
    return await get_service().get_conversation(_owner(request), conversation_id)


@router.post("/conversations/{conversation_id}/archive", response=ConversationSnapshot)
async def archive_conversation(request: HttpRequest, conversation_id: UUID) -> ConversationSnapshot:
    return await get_service().archive_conversation(_owner(request), conversation_id)


@router.post("/conversations/{conversation_id}/unarchive", response=ConversationSnapshot)
async def unarchive_conversation(
    request: HttpRequest, conversation_id: UUID
) -> ConversationSnapshot:
    return await get_service().unarchive_conversation(_owner(request), conversation_id)


@router.post("/conversations/{conversation_id}/pin", response=ConversationSnapshot)
async def pin_conversation(request: HttpRequest, conversation_id: UUID) -> ConversationSnapshot:
    return await get_service().pin_conversation(_owner(request), conversation_id)


@router.post("/conversations/{conversation_id}/unpin", response=ConversationSnapshot)
async def unpin_conversation(request: HttpRequest, conversation_id: UUID) -> ConversationSnapshot:
    return await get_service().unpin_conversation(_owner(request), conversation_id)


@router.post("/conversations/{conversation_id}/snooze", response=ConversationSnapshot)
async def snooze_conversation(
    request: HttpRequest, conversation_id: UUID, body: SnoozeBody
) -> ConversationSnapshot:
    return await get_service().snooze_conversation(
        _owner(request), conversation_id, until=body.until
    )


@router.post("/conversations/{conversation_id}/unsnooze", response=ConversationSnapshot)
async def unsnooze_conversation(
    request: HttpRequest, conversation_id: UUID
) -> ConversationSnapshot:
    return await get_service().unsnooze_conversation(_owner(request), conversation_id)


@router.delete("/conversations/{conversation_id}", response={204: None})
async def soft_delete_conversation(request: HttpRequest, conversation_id: UUID) -> tuple[int, None]:
    await get_service().soft_delete_conversation(_owner(request), conversation_id)
    return 204, None


# ---------------------------------------------------------------------------
# History pages
# ---------------------------------------------------------------------------


@router.get("/conversations/{conversation_id}/turns", response=Page[TurnProjection])
async def page_turns(
    request: HttpRequest,
    conversation_id: UUID,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[TurnProjection]:
    return await get_service().page_turns(
        _owner(request), conversation_id, cursor=cursor, limit=limit
    )


@router.get("/conversations/{conversation_id}/messages", response=Page[MessageProjection])
async def page_messages(
    request: HttpRequest,
    conversation_id: UUID,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[MessageProjection]:
    return await get_service().page_messages(
        _owner(request), conversation_id, cursor=cursor, limit=limit
    )


@router.get("/conversations/{conversation_id}/tools", response=Page[ToolProjection])
async def page_tools(
    request: HttpRequest,
    conversation_id: UUID,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[ToolProjection]:
    return await get_service().page_tools(
        _owner(request), conversation_id, cursor=cursor, limit=limit
    )


@router.get("/conversations/{conversation_id}/plans", response=Page[PlanProjection])
async def page_plans(
    request: HttpRequest,
    conversation_id: UUID,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[PlanProjection]:
    return await get_service().page_plans(
        _owner(request), conversation_id, cursor=cursor, limit=limit
    )


@router.get("/conversations/{conversation_id}/activity", response=Page[ActivityProjection])
async def page_activity(
    request: HttpRequest,
    conversation_id: UUID,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[ActivityProjection]:
    return await get_service().page_activity(
        _owner(request), conversation_id, cursor=cursor, limit=limit
    )


# ---------------------------------------------------------------------------
# Turn control
# ---------------------------------------------------------------------------


@router.post("/conversations/{conversation_id}/turns", response={202: SubmitTurnResult})
async def submit_turn(
    request: HttpRequest,
    conversation_id: UUID,
    body: SubmitTurnBody,
) -> tuple[int, SubmitTurnResult]:
    result = await get_service().submit_turn(
        _owner(request),
        conversation_id,
        prompt=body.prompt,
        idempotency_key=request.headers.get("Idempotency-Key") or "",
        model=body.model,
    )
    return 202, result


@router.patch("/conversations/{conversation_id}/queued-prompt", response=ConversationSnapshot)
async def edit_queued_prompt(
    request: HttpRequest, conversation_id: UUID, body: EditQueuedPromptBody
) -> ConversationSnapshot:
    return await get_service().edit_queued_prompt(
        _owner(request), conversation_id, prompt=body.prompt
    )


@router.delete(
    "/conversations/{conversation_id}/queued-prompt",
    response={200: CommandProjection, 204: None},
)
async def cancel_queued_prompt(
    request: HttpRequest, conversation_id: UUID
) -> tuple[int, CommandProjection | None]:
    cmd = await get_service().cancel_queued_prompt(_owner(request), conversation_id)
    if cmd is None:
        return 204, None
    return 200, cmd


@router.post("/conversations/{conversation_id}/steer", response={202: CommandProjection})
async def steer(
    request: HttpRequest,
    conversation_id: UUID,
    body: SteerBody,
) -> tuple[int, CommandProjection]:
    cmd = await get_service().steer(
        _owner(request),
        conversation_id,
        prompt=body.prompt,
        idempotency_key=request.headers.get("Idempotency-Key") or "",
    )
    return 202, cmd


@router.post("/conversations/{conversation_id}/interrupt", response={202: CommandProjection})
async def interrupt(request: HttpRequest, conversation_id: UUID) -> tuple[int, CommandProjection]:
    cmd = await get_service().interrupt(_owner(request), conversation_id)
    return 202, cmd


# ---------------------------------------------------------------------------
# Interactions
# ---------------------------------------------------------------------------


@router.get(
    "/conversations/{conversation_id}/interactions",
    response=Page[InteractionProjection],
)
async def list_interactions(
    request: HttpRequest,
    conversation_id: UUID,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[InteractionProjection]:
    return await get_service().list_pending_interactions(
        _owner(request), conversation_id, cursor=cursor, limit=limit
    )


@router.patch(
    "/conversations/{conversation_id}/interactions/{interaction_id}/draft",
    response=InteractionProjection,
)
async def update_interaction_draft(
    request: HttpRequest,
    conversation_id: UUID,
    interaction_id: UUID,
    body: InteractionDraftBody,
) -> InteractionProjection:
    return await get_service().update_interaction_draft(
        _owner(request),
        conversation_id,
        interaction_id,
        draft=body.draft,
    )


@router.post(
    "/conversations/{conversation_id}/interactions/{interaction_id}/resolve",
    response={202: CommandProjection},
)
async def resolve_interaction(
    request: HttpRequest,
    conversation_id: UUID,
    interaction_id: UUID,
    body: ResolveInteractionBody,
) -> tuple[int, CommandProjection]:
    cmd = await get_service().resolve_interaction(
        _owner(request),
        conversation_id,
        interaction_id,
        decision=body.decision,
        answers=body.answers,
    )
    return 202, cmd


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


@router.get("/conversations/{conversation_id}/events")
async def conversation_events(request: HttpRequest, conversation_id: UUID) -> StreamingHttpResponse:
    owner_id = _owner(request)
    last_event_id = parse_last_event_id(request.headers.get("Last-Event-ID"))
    service = get_service()
    await service.get_conversation(owner_id, conversation_id)
    stream = iter_sse(
        service,
        owner_id=owner_id,
        conversation_id=conversation_id,
        last_event_id=last_event_id,
    )
    response = StreamingHttpResponse(stream, content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response
