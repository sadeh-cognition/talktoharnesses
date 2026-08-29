"""The split service HTTP surface — identical across all harness splits.

Request bodies are strict frozen tth-types models, so routes validate the raw
JSON body directly (JSON-mode validation accepts UUID/datetime strings where
python-mode strict validation would not).
"""

from __future__ import annotations

from importlib.metadata import version as package_version
from uuid import UUID

from django.http import HttpRequest, StreamingHttpResponse
from ninja import NinjaAPI, Router
from tth_types.adapter import SteerRequest, TurnRequest
from tth_types.harness import InteractionAnswer
from tth_types.split_api import (
    CreateSessionRequest,
    ProbeRequest,
    ProbeResponse,
    SessionCreated,
    SplitHealth,
    SteerResult,
    TerminateRequest,
)

from tth_cursor import __version__ as split_version
from tth_cursor.auth import SplitTokenAuth
from tth_cursor.errors import register_exception_handlers
from tth_cursor.service import KIND, create_session, probe
from tth_cursor.sessions import get_session_store
from tth_cursor.sse import stream_frames

api = NinjaAPI(auth=SplitTokenAuth(), docs_url=None, openapi_url=None)
register_exception_handlers(api)

router = Router()


@router.get("/health", auth=None, response=SplitHealth)
def health(request: HttpRequest) -> SplitHealth:
    return SplitHealth(
        kind=KIND,
        split_version=split_version,
        tth_types_version=package_version("tth-types"),
        sessions=len(get_session_store()),
    )


@router.post("/probe", response=ProbeResponse)
async def probe_route(request: HttpRequest) -> ProbeResponse:
    body = ProbeRequest.model_validate_json(request.body)
    return await probe(body)


@router.post("/sessions", response={201: SessionCreated})
async def create_session_route(request: HttpRequest) -> tuple[int, SessionCreated]:
    body = CreateSessionRequest.model_validate_json(request.body)
    return 201, await create_session(body)


@router.get("/sessions/{session_id}/events")
async def session_events(request: HttpRequest, session_id: UUID) -> StreamingHttpResponse:
    store = get_session_store()
    entry = store.attach_stream(session_id)

    async def close_stream() -> None:
        await store.close(session_id, reason="stream_ended")

    response = StreamingHttpResponse(
        stream_frames(entry, close_stream),
        content_type="text/event-stream",
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@router.post("/sessions/{session_id}/turns", response={204: None})
async def submit_turn(request: HttpRequest, session_id: UUID) -> tuple[int, None]:
    body = TurnRequest.model_validate_json(request.body)
    entry = get_session_store().get(session_id)
    await entry.adapter.submit(entry.session, body)
    return 204, None


@router.post("/sessions/{session_id}/steer", response=SteerResult)
async def steer_turn(request: HttpRequest, session_id: UUID) -> SteerResult:
    body = SteerRequest.model_validate_json(request.body)
    entry = get_session_store().get(session_id)
    accepted = await entry.adapter.steer(entry.session, body)
    return SteerResult(accepted=accepted)


@router.post("/sessions/{session_id}/interrupt", response={204: None})
async def interrupt_session(request: HttpRequest, session_id: UUID) -> tuple[int, None]:
    entry = get_session_store().get(session_id)
    await entry.adapter.interrupt(entry.session)
    return 204, None


@router.post("/sessions/{session_id}/answers", response={204: None})
async def answer_interaction(request: HttpRequest, session_id: UUID) -> tuple[int, None]:
    body = InteractionAnswer.model_validate_json(request.body)
    entry = get_session_store().get(session_id)
    await entry.adapter.answer_interaction(entry.session, body)
    return 204, None


@router.delete("/sessions/{session_id}", response={204: None})
async def close_session(request: HttpRequest, session_id: UUID) -> tuple[int, None]:
    store = get_session_store()
    store.get(session_id)
    await store.close(session_id, reason="closed")
    return 204, None


@router.post("/sessions/{session_id}/terminate", response={204: None})
async def terminate_session(request: HttpRequest, session_id: UUID) -> tuple[int, None]:
    reason: str | None = None
    if request.body:
        reason = TerminateRequest.model_validate_json(request.body).reason
    await get_session_store().terminate(session_id, reason=reason)
    return 204, None


api.add_router("/v1", router)
