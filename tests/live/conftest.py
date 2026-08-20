"""In-process Django worker + official HTTP client for opt-in live gates."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import uvicorn
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.core.asgi import get_asgi_application
from tests.live.helpers import LiveHttp

from talktoharnesses.client import AsyncTalkToHarnessesClient
from talktoharnesses.django.asgi import (
    get_service,
    reset_service_for_tests,
    talktoharnesses_lifespan,
)
from talktoharnesses.django.auth import issue_token_sync


@pytest.fixture(scope="session")
def django_db_modify_db_settings(tmp_path_factory: pytest.TempPathFactory) -> None:
    """File-backed SQLite so the worker and concurrent ASGI requests share one DB."""
    from django.conf import settings

    db_path = tmp_path_factory.mktemp("live-db") / "live.sqlite3"
    settings.DATABASES["default"]["NAME"] = str(db_path)
    settings.DATABASES["default"]["TEST"]["NAME"] = str(db_path)
    settings.DATABASES["default"]["OPTIONS"] = {
        "timeout": 30,
        "transaction_mode": "IMMEDIATE",
    }


@pytest.fixture(autouse=True)
def enable_sqlite_wal(django_db_setup: None, django_db_blocker: Any) -> None:
    from django.db import connection

    with django_db_blocker.unblock(), connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")


@pytest.fixture
async def live_http(transactional_db: None, tmp_path: Path) -> AsyncIterator[LiveHttp]:
    """Start production composition and yield an official client over HTTP."""
    _ = transactional_db
    reset_service_for_tests()
    User: Any = get_user_model()
    user = await sync_to_async(User.objects.create_user)(
        username=f"live-{uuid4().hex[:8]}",
        password="x",
    )
    issued = await sync_to_async(issue_token_sync)(user)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    application = talktoharnesses_lifespan(cast(Any, get_asgi_application()))
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            lifespan="on",
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=5,
        )
    )
    server_task = asyncio.create_task(server.serve(sockets=[listener]))

    async def wait_until_started() -> None:
        while not server.started:
            if server_task.done():
                await server_task
                raise AssertionError("live HTTP server stopped before startup")
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_until_started(), timeout=10.0)
        async with AsyncTalkToHarnessesClient(
            f"http://127.0.0.1:{port}/api/v1/",
            token=issued.token,
            timeout=None,
        ) as client:
            health = await client.health()
            assert health["status"] == "ok"

            async def close_runtime(conversation_id: UUID) -> None:
                runtime = get_service()._runtime  # pyright: ignore[reportPrivateUsage]
                assert runtime.get_runtime(conversation_id) is not None, (
                    "live runtime was not running"
                )
                await runtime.close(conversation_id, reason="live-resume-gate")
                assert runtime.get_runtime(conversation_id) is None, (
                    "live runtime remained after close"
                )

            yield LiveHttp(
                client=client,
                workspace=tmp_path,
                close_runtime=close_runtime,
            )
    finally:
        server.should_exit = True
        try:
            await server_task
        finally:
            listener.close()
            reset_service_for_tests()
