"""Generic HarnessAdapter over a split service's HTTP + SSE API.

One instance per conversation runtime, mirroring the local-adapter invariant.
Contains zero harness-specific logic: everything kind-specific lives in the
split service this adapter talks to.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Protocol
from uuid import UUID

import httpx
from tth_types.adapter import (
    HarnessInteractionRequest,
    HarnessSession,
    ResumeSessionRequest,
    StartSessionRequest,
    SteerRequest,
    TurnRequest,
)
from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError
from tth_types.harness import (
    HarnessCapabilities,
    HarnessConfiguration,
    InteractionAnswer,
    LaunchSnapshot,
    VersionAdvisory,
)
from tth_types.split_api import (
    FRAME_END,
    FRAME_HARNESS_EVENT,
    FRAME_INTERACTION,
    FRAME_PROCESS,
    CreateSessionRequest,
    HarnessEventFrame,
    InteractionFrame,
    ProbeRequest,
    ProbeResponse,
    ProcessFrame,
    SessionCreated,
    SplitError,
    SteerResult,
    TerminateRequest,
)

from talktoharnesses._sse import SseDecoder
from talktoharnesses.domain.events import HarnessEvent
from talktoharnesses.remote.handle import RemoteProcessHandle
from talktoharnesses.remote.sandbox import rewrite_loopback_url

logger = logging.getLogger(__name__)

_UNARY_TIMEOUT = httpx.Timeout(90.0, connect=10.0)
_SESSION_CREATE_TIMEOUT = httpx.Timeout(None, connect=10.0)
_STREAM_TIMEOUT = httpx.Timeout(30.0, connect=10.0, read=None)


class SplitEndpointProvider(Protocol):
    """Resolves the base URL + shared secret of a split, booting it if needed."""

    async def endpoint(
        self,
        kind: HarnessKind,
        required_paths: tuple[str, ...] = (),
    ) -> ResolvedEndpoint: ...


class ResolvedEndpoint(Protocol):
    @property
    def base_url(self) -> str: ...

    @property
    def token(self) -> str | None: ...

    @property
    def loopback_alias(self) -> str | None: ...


def configuration_for_split(
    configuration: HarnessConfiguration, endpoint: ResolvedEndpoint
) -> HarnessConfiguration:
    """Rewrite loopback MCP server URLs for a split that runs in a container."""
    alias = endpoint.loopback_alias
    if alias is None or not configuration.mcp_servers:
        return configuration
    servers = tuple(
        server.model_copy(update={"url": rewrite_loopback_url(server.url, alias)})
        for server in configuration.mcp_servers
    )
    return configuration.model_copy(update={"mcp_servers": servers})


def _raise_split_error(response: httpx.Response) -> None:
    """Re-raise a split's SplitError body as a lossless DomainError.

    Codes outside ErrorCode (e.g. the split's request-validation
    ``validation_error``) still surface the split's message and details under
    PROTOCOL_ERROR so schema drift between proxy and split is diagnosable.
    """
    try:
        error = SplitError.model_validate_json(response.content)
    except ValueError:
        raise DomainError(
            ErrorCode.PROTOCOL_ERROR,
            f"split returned HTTP {response.status_code}",
            details={"status": response.status_code},
        ) from None
    try:
        code = ErrorCode(error.code)
    except ValueError:
        raise DomainError(
            ErrorCode.PROTOCOL_ERROR,
            f"split returned HTTP {response.status_code} ({error.code}): {error.message}",
            details={**error.details, "status": response.status_code, "split_code": error.code},
        ) from None
    raise DomainError(code, error.message, details=error.details)


class RemoteHarnessAdapter:
    """HarnessAdapter implementation proxying to one split service session."""

    # Duck-typed markers read by RuntimeManager: no local executable, no local
    # spawn (the split owns the process), remote seen-import before start.
    sdk_managed = True
    remote = True

    def __init__(
        self,
        kind: HarnessKind,
        endpoints: SplitEndpointProvider,
        *,
        adapter_version: str = "0",
        client_factory: type[httpx.AsyncClient] | None = None,
    ) -> None:
        self.kind = kind
        self._endpoints = endpoints
        self._adapter_version = adapter_version
        self._client_type = client_factory or httpx.AsyncClient
        self._client: httpx.AsyncClient | None = None
        self._redaction_patterns: tuple[str, ...] = ()
        self._required_paths: tuple[str, ...] = ()
        self._seen_native: set[str] = set()
        self._seen_offsets: set[str] = set()
        self._session_id: UUID | None = None
        self._probe_launch: LaunchSnapshot | None = None
        self._probe_advisory: VersionAdvisory | None = None
        self._handle: RemoteProcessHandle | None = None
        self._endpoint: ResolvedEndpoint | None = None
        self._closed = False

    # ------------------------------------------------------------------
    # Duck-typed hooks used by RuntimeManager / CommandProcessor
    # ------------------------------------------------------------------

    def set_redaction_patterns(self, patterns: tuple[str, ...]) -> None:
        self._redaction_patterns = patterns

    def import_seen(
        self,
        native_ids: frozenset[str],
        stream_offsets: frozenset[str],
    ) -> None:
        self._seen_native.update(native_ids)
        self._seen_offsets.update(stream_offsets)

    def export_seen(self) -> tuple[frozenset[str], frozenset[str]]:
        return frozenset(self._seen_native), frozenset(self._seen_offsets)

    def last_probe_launch(self) -> LaunchSnapshot | None:
        return self._probe_launch

    def last_probe_advisory(self) -> VersionAdvisory | None:
        return self._probe_advisory

    @property
    def process_handle(self) -> RemoteProcessHandle | None:
        return self._handle

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    async def _resolved_endpoint(self) -> ResolvedEndpoint:
        if self._endpoint is None:
            self._endpoint = await self._endpoints.endpoint(self.kind, self._required_paths)
        return self._endpoint

    async def _client_for_split(self) -> httpx.AsyncClient:
        if self._client is None:
            resolved = await self._resolved_endpoint()
            headers: dict[str, str] = {}
            if resolved.token:
                headers["X-TTH-Split-Token"] = resolved.token
            self._client = self._client_type(
                base_url=resolved.base_url,
                headers=headers,
                timeout=_UNARY_TIMEOUT,
            )
        return self._client

    async def _post(
        self,
        path: str,
        body_json: str | None = None,
        *,
        timeout: httpx.Timeout = _UNARY_TIMEOUT,
    ) -> httpx.Response:
        client = await self._client_for_split()
        try:
            response = await client.post(
                path,
                content=body_json,
                headers={"Content-Type": "application/json"} if body_json else None,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                f"split request failed: {exc}",
                details={"kind": self.kind.value, "path": path},
            ) from exc
        if response.status_code >= 400:
            _raise_split_error(response)
        return response

    def _require_session_id(self) -> UUID:
        if self._session_id is None:
            raise DomainError(ErrorCode.INVALID_STATE, "remote adapter has no active session")
        return self._session_id

    # ------------------------------------------------------------------
    # HarnessAdapter protocol
    # ------------------------------------------------------------------

    async def _split_configuration(
        self, configuration: HarnessConfiguration
    ) -> HarnessConfiguration:
        return configuration_for_split(configuration, await self._resolved_endpoint())

    async def probe(self, config: HarnessConfiguration) -> HarnessCapabilities:
        self._required_paths = (config.working_directory, *config.workspace_roots)
        request = ProbeRequest(
            configuration=await self._split_configuration(config),
            adapter_version=self._adapter_version,
            redaction_patterns=self._redaction_patterns,
        )
        response = await self._post("/v1/probe", request.model_dump_json())
        probe = ProbeResponse.model_validate_json(response.content)
        self._probe_launch = probe.launch
        self._probe_advisory = probe.advisory
        return probe.capabilities

    async def start(self, request: StartSessionRequest) -> HarnessSession:
        return await self._create_session(
            mode="start",
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            configuration=request.configuration,
            native_session_id=None,
        )

    async def resume(self, request: ResumeSessionRequest) -> HarnessSession:
        return await self._create_session(
            mode="resume",
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            configuration=request.configuration,
            native_session_id=request.native_session_id,
        )

    async def _create_session(
        self,
        *,
        mode: str,
        conversation_id: UUID,
        binding_id: UUID,
        configuration: HarnessConfiguration,
        native_session_id: str | None,
    ) -> HarnessSession:
        self._required_paths = (
            configuration.working_directory,
            *configuration.workspace_roots,
        )
        body = CreateSessionRequest.model_validate(
            {
                "mode": mode,
                "conversation_id": conversation_id,
                "binding_id": binding_id,
                "configuration": await self._split_configuration(configuration),
                "native_session_id": native_session_id,
                "adapter_version": self._adapter_version,
                "redaction_patterns": self._redaction_patterns,
                "seen_native_ids": tuple(sorted(self._seen_native)),
                "seen_stream_offsets": tuple(sorted(self._seen_offsets)),
            }
        )
        # The split applies separate bounded probe, spawn, and start/resume
        # budgets. Keep the proxy request alive across the whole sequence and
        # retain its client-chosen id so rollback can always address it.
        self._session_id = body.session_id
        response = await self._post(
            "/v1/sessions",
            body.model_dump_json(),
            timeout=_SESSION_CREATE_TIMEOUT,
        )
        created = SessionCreated.model_validate_json(response.content)
        if created.session_id != body.session_id:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                "split returned a different session id",
                details={"kind": self.kind.value},
            )
        self._session_id = created.session_id
        self._probe_launch = created.launch
        if created.pid is not None:
            self._handle = RemoteProcessHandle(
                pid=created.pid,
                terminate=self._terminate_session,
            )
        return created.session

    async def submit(self, session: HarnessSession, request: TurnRequest) -> None:
        sid = self._require_session_id()
        await self._post(f"/v1/sessions/{sid}/turns", request.model_dump_json())

    async def steer(self, session: HarnessSession, request: SteerRequest) -> bool:
        sid = self._require_session_id()
        response = await self._post(f"/v1/sessions/{sid}/steer", request.model_dump_json())
        return SteerResult.model_validate_json(response.content).accepted

    async def interrupt(self, session: HarnessSession) -> None:
        sid = self._require_session_id()
        logger.info(
            "split interrupt kind=%s session=%s conversation=%s",
            self.kind.value,
            sid,
            session.conversation_id,
        )
        await self._post(f"/v1/sessions/{sid}/interrupt")
        logger.info("split interrupt accepted kind=%s session=%s", self.kind.value, sid)

    async def answer_interaction(
        self,
        session: HarnessSession,
        answer: InteractionAnswer,
    ) -> None:
        sid = self._require_session_id()
        await self._post(f"/v1/sessions/{sid}/answers", answer.model_dump_json())

    def events(
        self,
        session: HarnessSession,
    ) -> AsyncIterator[HarnessEvent | HarnessInteractionRequest]:
        sid = self._require_session_id()

        async def _gen() -> AsyncIterator[HarnessEvent | HarnessInteractionRequest]:
            client = await self._client_for_split()
            decoder = SseDecoder()
            frames: dict[str, int] = {}
            outcome = "generator closed"
            logger.info(
                "split event stream opening kind=%s session=%s conversation=%s",
                self.kind.value,
                sid,
                session.conversation_id,
            )
            try:
                async with client.stream(
                    "GET",
                    f"/v1/sessions/{sid}/events",
                    timeout=_STREAM_TIMEOUT,
                ) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        _raise_split_error(response)
                    async for chunk in response.aiter_bytes():
                        for sse in decoder.feed(chunk):
                            name = sse.event or "?"
                            frames[name] = frames.get(name, 0) + 1
                            item = self._decode_frame(sse.event, sse.data)
                            if item is _STREAM_END:
                                outcome = "end frame"
                                return
                            if isinstance(item, HarnessInteractionRequest):
                                logger.info(
                                    "split interaction received kind=%s session=%s id=%s",
                                    self.kind.value,
                                    sid,
                                    item.payload.interaction_id,
                                )
                            if item is not None:
                                yield item  # type: ignore[misc]
                    outcome = "response ended"
            except httpx.HTTPError as exc:
                outcome = f"http error: {exc!r}"
                logger.warning(
                    "split event stream dropped kind=%s session=%s: %s",
                    self.kind.value,
                    sid,
                    exc,
                )
            finally:
                logger.info(
                    "split event stream closed kind=%s session=%s outcome=%s frames=%s",
                    self.kind.value,
                    sid,
                    outcome,
                    frames,
                )
                if self._handle is not None:
                    self._handle.mark_stream_closed()

        return _gen()

    def _decode_frame(
        self,
        event_name: str | None,
        data: str,
    ) -> HarnessEvent | HarnessInteractionRequest | object | None:
        if event_name == FRAME_HARNESS_EVENT:
            frame = HarnessEventFrame.model_validate_json(data)
            self._seen_native.update(frame.new_native_ids)
            self._seen_offsets.update(frame.new_stream_offsets)
            return frame.item
        if event_name == FRAME_INTERACTION:
            interaction = InteractionFrame.model_validate_json(data)
            self._seen_native.update(interaction.new_native_ids)
            self._seen_offsets.update(interaction.new_stream_offsets)
            return HarnessInteractionRequest(
                payload=interaction.payload,
                provider_correlation=interaction.provider_correlation,
            )
        if event_name == FRAME_PROCESS:
            process = ProcessFrame.model_validate_json(data)
            if self._handle is not None:
                self._handle.on_frame(process)
            return None
        if event_name == FRAME_END:
            return _STREAM_END
        return None

    async def close(self, session: HarnessSession) -> None:
        if self._closed:
            return
        self._closed = True
        sid = self._session_id
        try:
            # No client means no request ever reached the split, so there is
            # no remote session to delete. Resolving an endpoint here would
            # also re-enter SandboxManager.endpoint() during startup rollback
            # and could kick off a fresh prepare (image build) for a sandbox
            # that just failed.
            if sid is not None and self._client is not None:
                client = self._client
                try:
                    response = await client.delete(f"/v1/sessions/{sid}")
                except httpx.HTTPError as exc:
                    logger.warning(
                        "split session close failed kind=%s session=%s: %s",
                        self.kind.value,
                        sid,
                        exc,
                    )
                else:
                    if response.status_code >= 400 and response.status_code != 404:
                        logger.warning(
                            "split session close rejected kind=%s session=%s status=%s",
                            self.kind.value,
                            sid,
                            response.status_code,
                        )
        finally:
            if self._handle is not None:
                self._handle.mark_stream_closed()
            await self.aclose()

    async def aclose(self) -> None:
        """Release the HTTP client without touching any split session.

        Probe-only callers (service probe, readiness monitor) never create a
        session but do open the client; they call this to avoid leaking a
        connection pool per probe.
        """
        if self._client is None:
            return
        client = self._client
        self._client = None
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            logger.debug("split client close failed", exc_info=True)

    async def _terminate_session(self, reason: str | None) -> None:
        sid = self._session_id
        if sid is None:
            return
        body = TerminateRequest(reason=reason).model_dump_json()
        try:
            await self._post(f"/v1/sessions/{sid}/terminate", body)
        except DomainError as exc:
            if exc.code is not ErrorCode.NOT_FOUND:
                raise


_STREAM_END = object()
