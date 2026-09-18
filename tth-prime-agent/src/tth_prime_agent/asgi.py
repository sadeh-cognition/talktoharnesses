"""ASGI entrypoint with a lifespan wrapper that closes live sessions on shutdown."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, cast

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tth_prime_agent.settings")

from tth_prime_agent.telemetry import configure_opentelemetry, instrument_django  # noqa: E402

configure_opentelemetry()
instrument_django()

from django.core.asgi import get_asgi_application  # noqa: E402

from tth_prime_agent.sessions import get_session_store  # noqa: E402
from tth_prime_agent.shared.private_env import seal  # noqa: E402

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def _lifespan(app: ASGIApp) -> ASGIApp:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "lifespan":
            await app(scope, receive, send)
            return
        while True:
            message = await receive()
            msg_type = message.get("type")
            if msg_type == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif msg_type == "lifespan.shutdown":
                await get_session_store().shutdown()
                await send({"type": "lifespan.shutdown.complete"})
                return

    return asgi


application = _lifespan(cast(ASGIApp, get_asgi_application()))
# Django is configured now; harness children must not inherit the split's
# settings module or the proxy's shared secret.
seal()
