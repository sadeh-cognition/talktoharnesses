"""Typed-ish Codex app-server client over :class:`JsonRpcPeer`."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from talktoharnesses.codex.methods import ClientMethods, ClientNotifications, ServerRequests
from talktoharnesses.transports.stdio_jsonrpc import JsonRpcPeer

JsonDict = dict[str, Any]
NotificationHandler = Callable[[str, Any], Awaitable[None] | None]
ServerRequestHandler = Callable[[str, Any], Awaitable[Any]]


class CodexAppServerClient:
    """Thin facade: initialize / thread / turn RPCs + notification routing."""

    def __init__(self, peer: JsonRpcPeer) -> None:
        self._peer = peer

    @property
    def peer(self) -> JsonRpcPeer:
        return self._peer

    def start(self) -> None:
        self._peer.start()

    def on_notification(self, handler: NotificationHandler) -> None:
        self._peer.on_notification(handler)

    def on_server_request(self, method: str, handler: ServerRequestHandler) -> None:
        self._peer.on_request(method, handler)

    async def initialize(
        self,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = 30.0,
    ) -> Any:
        return await self._peer.request(
            ClientMethods.INITIALIZE,
            dict(params) if params is not None else {},
            timeout=timeout,
        )

    async def initialized(self) -> None:
        await self._peer.notify(ClientNotifications.INITIALIZED, {})

    async def thread_start(
        self,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = 60.0,
    ) -> Any:
        return await self._peer.request(
            ClientMethods.THREAD_START,
            dict(params) if params is not None else {},
            timeout=timeout,
        )

    async def thread_resume(
        self,
        params: Mapping[str, Any],
        *,
        timeout: float | None = 60.0,
    ) -> Any:
        return await self._peer.request(
            ClientMethods.THREAD_RESUME,
            dict(params),
            timeout=timeout,
        )

    async def turn_start(
        self,
        params: Mapping[str, Any],
        *,
        timeout: float | None = 60.0,
    ) -> Any:
        return await self._peer.request(
            ClientMethods.TURN_START,
            dict(params),
            timeout=timeout,
        )

    async def turn_interrupt(
        self,
        params: Mapping[str, Any],
        *,
        timeout: float | None = 30.0,
    ) -> Any:
        return await self._peer.request(
            ClientMethods.TURN_INTERRUPT,
            dict(params),
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._peer.aclose()

    def register_approval_handlers(
        self,
        *,
        on_command_approval: ServerRequestHandler,
        on_file_change_approval: ServerRequestHandler,
        on_user_input: ServerRequestHandler | None = None,
        on_permissions_approval: ServerRequestHandler | None = None,
    ) -> None:
        """Wire the standard server→client approval request methods."""
        self.on_server_request(
            ServerRequests.COMMAND_EXECUTION_REQUEST_APPROVAL, on_command_approval
        )
        self.on_server_request(
            ServerRequests.FILE_CHANGE_REQUEST_APPROVAL, on_file_change_approval
        )
        if on_user_input is not None:
            self.on_server_request(ServerRequests.TOOL_REQUEST_USER_INPUT, on_user_input)
        if on_permissions_approval is not None:
            self.on_server_request(
                ServerRequests.PERMISSIONS_REQUEST_APPROVAL, on_permissions_approval
            )
