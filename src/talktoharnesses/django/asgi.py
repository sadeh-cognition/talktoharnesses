"""ASGI lifespan composition for talktoharnesses.

Host projects wire this into their own ``asgi.py``::

    from django.core.asgi import get_asgi_application
    from talktoharnesses.django.asgi import talktoharnesses_lifespan

    application = talktoharnesses_lifespan(get_asgi_application())

Run with Uvicorn (host-owned process)::

    uvicorn host.asgi:application --host 127.0.0.1

Authentication authorizes the client-facing API; it does not configure the
proxy-to-split security boundary. Harness programs run in proxy-managed Docker
sandboxes spawned on demand and tracked in the database.

The host owns Django settings, migrations, URL inclusion, and Uvicorn
invocation. This package does not auto-run migrations, provide a CLI, or
modify host middleware/settings.
"""

from __future__ import annotations

import logging
import os
import socket
from collections.abc import Awaitable, Callable, MutableMapping
from datetime import UTC, datetime
from typing import Any

from talktoharnesses.application.service import TalkToHarnessesService
from talktoharnesses.django.auth import validate_jwt_settings
from talktoharnesses.django.broker import DjangoCommittedEventBroker
from talktoharnesses.django.persistence import DjangoPersistence
from talktoharnesses.django.sandbox_policies import DjangoSandboxPolicyStore
from talktoharnesses.django.sandbox_store import DjangoSandboxStore
from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.remote.registry import build_remote_adapter_registry
from talktoharnesses.remote.sandbox import (
    SandboxConfig,
    ensure_docker_cli_available,
)
from talktoharnesses.remote.scoped_sandboxes import ScopedSandboxManager
from talktoharnesses.runtime.manager import RuntimeManager
from talktoharnesses.runtime.policy import RuntimePolicy

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# One service instance per process/event loop (set on lifespan startup).
_service: TalkToHarnessesService | None = None


def get_service() -> TalkToHarnessesService:
    """Return the process-local service started by :func:`talktoharnesses_lifespan`.

    Fails closed when the ASGI wrapper was not installed or startup has not
    completed. API routers must use this accessor rather than constructing a
    second service.
    """
    if _service is None:
        raise DomainError(
            ErrorCode.INVALID_STATE,
            "talktoharnesses service is not started; "
            "wrap the ASGI app with talktoharnesses_lifespan",
        )
    return _service


def reset_service_for_tests() -> None:
    """Clear the process-local service handle (tests only)."""
    global _service
    _service = None


def _utc_clock() -> datetime:
    return datetime.now(UTC)


def _build_service() -> TalkToHarnessesService:
    """Construct the default production composition for one ASGI process."""
    # Fail closed on invalid JWT config before accepting HTTP traffic.
    validate_jwt_settings()
    # Every harness runs in a Docker sandbox; refuse to start without the CLI.
    ensure_docker_cli_available()

    persistence = DjangoPersistence()
    # Every kind is remote: its Docker sandbox is spawned on demand and
    # tracked in the sandbox store so restarts reattach to it.
    policies = DjangoSandboxPolicyStore()
    sandboxes = ScopedSandboxManager(
        SandboxConfig.from_env(), store=DjangoSandboxStore(), policies=policies
    )
    registry = build_remote_adapter_registry(sandboxes)
    broker = DjangoCommittedEventBroker()
    runtime = RuntimeManager(
        persistence, registry, policy=RuntimePolicy.from_env(), clock=_utc_clock
    )
    return TalkToHarnessesService(
        persistence,
        registry,
        broker,
        _utc_clock,
        runtime,
        readiness_spawn_gate=sandboxes.is_running,
        sandbox_policies=policies,
    )


def _worker_id() -> str:
    host = socket.gethostname()
    return f"asgi-{host}-{os.getpid()}"


async def _startup() -> TalkToHarnessesService:
    global _service
    service = _build_service()
    await service.start(_worker_id())
    _service = service
    logger.info("talktoharnesses service started worker_id=%s", _worker_id())
    return service


async def _shutdown() -> None:
    global _service
    service = _service
    _service = None
    if service is None:
        return
    try:
        await service.stop()
    finally:
        logger.info("talktoharnesses service stopped")


def talktoharnesses_lifespan(app: ASGIApp) -> ASGIApp:
    """Wrap a Django ASGI application with talktoharnesses worker lifespan.

    HTTP/WebSocket scopes pass through unchanged. Lifespan startup constructs
    and starts one :class:`TalkToHarnessesService` per process; failure is
    reported to the ASGI server so traffic is not served without a worker.
    """

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "lifespan":
            await app(scope, receive, send)
            return

        while True:
            message = await receive()
            msg_type = message.get("type")
            if msg_type == "lifespan.startup":
                try:
                    await _startup()
                except Exception:
                    logger.exception("talktoharnesses lifespan startup failed")
                    await send(
                        {
                            "type": "lifespan.startup.failed",
                            "message": "startup failed",
                        }
                    )
                    return
                await send({"type": "lifespan.startup.complete"})
            elif msg_type == "lifespan.shutdown":
                try:
                    await _shutdown()
                except Exception:
                    logger.exception("talktoharnesses lifespan shutdown failed")
                    await send(
                        {
                            "type": "lifespan.shutdown.failed",
                            "message": "shutdown failed",
                        }
                    )
                    return
                await send({"type": "lifespan.shutdown.complete"})
                return

    return asgi
