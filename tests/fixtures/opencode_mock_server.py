#!/usr/bin/env python3
"""Minimal OpenCode HTTP + SSE mock server for driver tests.

Endpoints:
  POST /session
  POST /session/{id}/prompt_async
  POST /session/{id}/abort
  POST /permission/{id}/reply
  GET  /event  (SSE)

Env:
  TALKTOHARNESSES_OPENCODE_HOST (default 127.0.0.1)
  TALKTOHARNESSES_OPENCODE_PORT (required)
  TALKTOHARNESSES_OPENCODE_APPROVAL=1
  TALKTOHARNESSES_OPENCODE_DECISIONS — path to append permission replies
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

HOST = os.environ.get("TALKTOHARNESSES_OPENCODE_HOST", "127.0.0.1")
PORT = int(os.environ["TALKTOHARNESSES_OPENCODE_PORT"])
WANT_APPROVAL = os.environ.get("TALKTOHARNESSES_OPENCODE_APPROVAL") == "1"
DECISIONS_PATH = os.environ.get("TALKTOHARNESSES_OPENCODE_DECISIONS")

_sessions: dict[str, dict[str, Any]] = {}
_subscribers: list[asyncio.Queue[dict[str, Any] | None]] = []
_permission_waiters: dict[str, asyncio.Future[str]] = {}


async def broadcast(event: dict[str, Any]) -> None:
    for q in list(_subscribers):
        await q.put(event)


async def create_session(request: Request) -> JSONResponse:
    body = await request.json() if request.headers.get("content-type") else {}
    sid = f"ses_{len(_sessions) + 1}"
    _sessions[sid] = {"id": sid, "directory": body.get("directory"), "title": body.get("title")}
    await broadcast(
        {
            "type": "session.created",
            "properties": {"info": {"id": sid}},
        }
    )
    return JSONResponse({"id": sid, "directory": body.get("directory")})


async def prompt_async(request: Request) -> Response:
    sid = request.path_params["sessionID"]
    body = await request.json()
    parts = body.get("parts") or []
    text = ""
    for p in parts:
        if isinstance(p, dict) and p.get("type") == "text":
            text += str(p.get("text") or "")

    async def _run() -> None:
        await broadcast(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": sid,
                    "status": {"type": "busy"},
                },
            }
        )
        for chunk in ("Hel", "lo", " OK"):
            await broadcast(
                {
                    "type": "message.part.delta",
                    "properties": {
                        "sessionID": sid,
                        "partID": "part-1",
                        "delta": chunk,
                    },
                }
            )
            await asyncio.sleep(0.01)

        if WANT_APPROVAL:
            pid = "perm-1"
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[str] = loop.create_future()
            _permission_waiters[pid] = fut
            await broadcast(
                {
                    "type": "permission.asked",
                    "properties": {
                        "id": pid,
                        "sessionID": sid,
                        "permission": "bash",
                        "patterns": ["ls -la"],
                        "metadata": {},
                    },
                }
            )
            try:
                await asyncio.wait_for(fut, timeout=10.0)
            except TimeoutError:
                pass
            finally:
                _permission_waiters.pop(pid, None)

        await broadcast(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": sid,
                    "status": {"type": "idle"},
                },
            }
        )

    asyncio.create_task(_run())
    return Response(status_code=204)


async def abort_session(request: Request) -> JSONResponse:
    sid = request.path_params["sessionID"]
    await broadcast(
        {
            "type": "session.status",
            "properties": {"sessionID": sid, "status": {"type": "idle"}},
        }
    )
    return JSONResponse({"ok": True})


async def permission_reply(request: Request) -> JSONResponse:
    pid = request.path_params["requestID"]
    body = await request.json()
    reply = body.get("reply")
    if pid not in _permission_waiters:
        # Match a real server: replying to an unknown permission is an error,
        # not a silent success.
        return JSONResponse({"error": f"unknown permission {pid}"}, status_code=404)
    if DECISIONS_PATH:
        path = Path(DECISIONS_PATH)
        existing: list[Any] = []
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
        existing.append({"requestID": pid, "reply": reply})
        path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    fut = _permission_waiters.get(pid)
    if fut is not None and not fut.done():
        fut.set_result(str(reply))
    await broadcast(
        {
            "type": "permission.replied",
            "properties": {"id": pid, "reply": reply},
        }
    )
    return JSONResponse({"ok": True})


async def event_stream(request: Request) -> Response:
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    _subscribers.append(queue)

    async def gen() -> Any:
        try:
            yield "event: server.connected\ndata: {\"type\":\"server.connected\"}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                if item is None:
                    break
                payload = json.dumps(item, separators=(",", ":"))
                yield f"data: {payload}\n\n"
        finally:
            with contextlib.suppress(ValueError):
                _subscribers.remove(queue)

    import contextlib

    from starlette.responses import StreamingResponse

    return StreamingResponse(gen(), media_type="text/event-stream")


app = Starlette(
    routes=[
        Route("/session", create_session, methods=["POST"]),
        Route("/session/{sessionID}/prompt_async", prompt_async, methods=["POST"]),
        Route("/session/{sessionID}/abort", abort_session, methods=["POST"]),
        Route("/permission/{requestID}/reply", permission_reply, methods=["POST"]),
        Route("/event", event_stream, methods=["GET"]),
    ]
)


def main() -> None:
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
