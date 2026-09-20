"""Shared bearer authentication for the client's HTTP and SSE requests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable

import httpx


class TokenProviderAuth(httpx.Auth):
    def __init__(
        self,
        provider: Callable[[], Awaitable[str]],
        on_rejected: Callable[[str], Awaitable[None]] | None,
    ) -> None:
        self._provider = provider
        self._on_rejected = on_rejected

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        token = await self._provider()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code != 401:
            return

        refreshed = await self._provider()
        if refreshed != token:
            token = refreshed
            request.headers["Authorization"] = f"Bearer {token}"
            response = yield request

        if response.status_code == 401 and self._on_rejected is not None:
            await self._on_rejected(token)
