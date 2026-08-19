"""Official async HTTP client for the talktoharnesses Django API surface."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from datetime import datetime
from typing import Any, TypeAlias, TypeVar
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, TypeAdapter

from talktoharnesses import __version__
from talktoharnesses._sse import SseDecoder
from talktoharnesses.domain.enums import ApprovalDecision
from talktoharnesses.domain.events import (
    ConversationEvent,
    ConversationMetadataChangedPayload,
    conversation_event_adapter,
)
from talktoharnesses.domain.models import (
    ActivityProjection,
    ApprovalRuleInput,
    ApprovalRuleProjection,
    CommandProjection,
    ConversationSearchHit,
    ConversationShell,
    ConversationSnapshot,
    ErrorProjection,
    HarnessConfiguration,
    HarnessModeInfo,
    HarnessModelInfo,
    HarnessProbeProjection,
    HarnessProjection,
    InteractionAuditProjection,
    InteractionProjection,
    MessageProjection,
    Page,
    PlanProjection,
    ReadinessProjection,
    RetentionPolicyProjection,
    RetentionPreviewProjection,
    SubmitTurnResult,
    SyncProjection,
    TokenProjection,
    ToolProjection,
    TurnProjection,
)
from talktoharnesses.domain.transcripts import TranscriptDocument

try:
    import httpx
except ModuleNotFoundError as exc:  # pragma: no cover - exercised in packaging isolation tests
    if exc.name == "httpx":
        raise ModuleNotFoundError(
            "httpx is required for talktoharnesses.client; "
            'install with: pip install "talktoharnesses[client]"'
        ) from exc
    raise

__all__ = [
    "APIError",
    "AsyncTalkToHarnessesClient",
    "ConversationStreamItem",
]

ConversationStreamItem: TypeAlias = ConversationEvent | ConversationSnapshot | SyncProjection

_TModel = TypeVar("_TModel", bound=BaseModel)

_PAGE_HARNESS = TypeAdapter(Page[HarnessProjection])
_PAGE_CONVERSATION = TypeAdapter(Page[ConversationShell])
_PAGE_SEARCH = TypeAdapter(Page[ConversationSearchHit])
_PAGE_TURN = TypeAdapter(Page[TurnProjection])
_PAGE_MESSAGE = TypeAdapter(Page[MessageProjection])
_PAGE_TOOL = TypeAdapter(Page[ToolProjection])
_PAGE_PLAN = TypeAdapter(Page[PlanProjection])
_PAGE_ACTIVITY = TypeAdapter(Page[ActivityProjection])
_PAGE_INTERACTION = TypeAdapter(Page[InteractionProjection])
_PAGE_APPROVAL_RULE = TypeAdapter(Page[ApprovalRuleProjection])
_PAGE_INTERACTION_AUDIT = TypeAdapter(Page[InteractionAuditProjection])
_HARNESS_MODELS = TypeAdapter(tuple[HarnessModelInfo, ...])
_HARNESS_MODES = TypeAdapter(tuple[HarnessModeInfo, ...])
_HEALTH = TypeAdapter(dict[str, str])

_BACKOFF_CAP_S = 30.0
_BACKOFF_INITIAL_S = 1.0


class _UnsetType:
    __slots__ = ()


_UNSET = _UnsetType()
_Timeout: TypeAlias = float | None | _UnsetType


class APIError(Exception):
    """Typed failure for non-success HTTP responses from the API."""

    status_code: int
    code: str | None
    message: str

    def __init__(self, status_code: int, code: str | None, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(self._format())

    def _format(self) -> str:
        if self.code is not None:
            return f"HTTP {self.status_code} [{self.code}]: {self.message}"
        return f"HTTP {self.status_code}: {self.message}"

    @classmethod
    def from_response(cls, response: httpx.Response) -> APIError:
        try:
            projection = ErrorProjection.model_validate_json(response.content)
        except Exception:  # noqa: BLE001 — fall back to generic safe message
            return cls(response.status_code, None, "HTTP request failed")
        return cls(response.status_code, projection.code, projection.message)


def _normalize_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("base_url must be an absolute http or https URL")
    return base_url.rstrip("/") + "/"


class AsyncTalkToHarnessesClient:
    """Async client for the versioned talktoharnesses HTTP/SSE API."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float | None = 30.0,
    ) -> None:
        self._base_url = _normalize_base_url(base_url)
        self._token = token
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={"User-Agent": f"talktoharnesses/{__version__}"},
        )

    @property
    def token(self) -> str | None:
        return self._token

    async def __aenter__(self) -> AsyncTalkToHarnessesClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _request_headers(
        self,
        *,
        extra: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        if extra:
            headers.update(extra)
        return headers

    def _resolved_timeout(self, timeout: _Timeout) -> float | None:
        if isinstance(timeout, _UnsetType):
            return self._timeout
        return timeout

    async def _request(
        self,
        method: str,
        path: str,
        *,
        accepted: int | Sequence[int],
        params: Mapping[str, str | int | bool | None] | None = None,
        json: Any | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: _Timeout = _UNSET,
    ) -> httpx.Response:
        accepted_set = {accepted} if isinstance(accepted, int) else set(accepted)
        query: dict[str, str | int | bool] | None = None
        if params is not None:
            filtered = {key: value for key, value in params.items() if value is not None}
            query = filtered or None
        response = await self._client.request(
            method,
            path,
            params=query,
            json=json,
            headers=self._request_headers(extra=headers),
            timeout=self._resolved_timeout(timeout),
        )
        if response.status_code not in accepted_set:
            raise APIError.from_response(response)
        return response

    @staticmethod
    def _parse_model(model: type[_TModel], response: httpx.Response) -> _TModel:
        return model.model_validate_json(response.content)

    # ------------------------------------------------------------------
    # System and authentication
    # ------------------------------------------------------------------

    async def health(self, *, timeout: _Timeout = _UNSET) -> dict[str, str]:
        response = await self._request("GET", "health", accepted=200, timeout=timeout)
        return _HEALTH.validate_json(response.content)

    async def ready(self, *, timeout: _Timeout = _UNSET) -> ReadinessProjection:
        response = await self._request("GET", "ready", accepted=(200, 503), timeout=timeout)
        return self._parse_model(ReadinessProjection, response)

    async def rotate_token(self, *, timeout: _Timeout = _UNSET) -> TokenProjection:
        response = await self._request("POST", "auth/token/rotate", accepted=200, timeout=timeout)
        projection = self._parse_model(TokenProjection, response)
        self._token = projection.token
        return projection

    async def revoke_token(self, *, timeout: _Timeout = _UNSET) -> None:
        await self._request("POST", "auth/token/revoke", accepted=204, timeout=timeout)
        self._token = None

    # ------------------------------------------------------------------
    # Harnesses
    # ------------------------------------------------------------------

    async def list_harnesses(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        timeout: _Timeout = _UNSET,
    ) -> Page[HarnessProjection]:
        response = await self._request(
            "GET",
            "harnesses",
            accepted=200,
            params={"cursor": cursor, "limit": limit},
            timeout=timeout,
        )
        return _PAGE_HARNESS.validate_json(response.content)

    async def create_harness(
        self,
        *,
        name: str,
        configuration: HarnessConfiguration,
        timeout: _Timeout = _UNSET,
    ) -> HarnessProjection:
        response = await self._request(
            "POST",
            "harnesses",
            accepted=201,
            json={
                "name": name,
                "configuration": configuration.model_dump(mode="json", exclude_none=True),
            },
            timeout=timeout,
        )
        return self._parse_model(HarnessProjection, response)

    async def get_harness(
        self,
        harness_id: UUID,
        *,
        timeout: _Timeout = _UNSET,
    ) -> HarnessProjection:
        response = await self._request(
            "GET",
            f"harnesses/{harness_id}",
            accepted=200,
            timeout=timeout,
        )
        return self._parse_model(HarnessProjection, response)

    async def delete_harness(self, harness_id: UUID, *, timeout: _Timeout = _UNSET) -> None:
        await self._request(
            "DELETE",
            f"harnesses/{harness_id}",
            accepted=204,
            timeout=timeout,
        )

    async def probe_harness(
        self,
        harness_id: UUID,
        *,
        timeout: _Timeout = _UNSET,
    ) -> HarnessProbeProjection:
        response = await self._request(
            "POST",
            f"harnesses/{harness_id}/probe",
            accepted=200,
            timeout=timeout,
        )
        return self._parse_model(HarnessProbeProjection, response)

    async def get_harness_capabilities(
        self,
        harness_id: UUID,
        *,
        timeout: _Timeout = _UNSET,
    ) -> HarnessProbeProjection:
        response = await self._request(
            "GET",
            f"harnesses/{harness_id}/capabilities",
            accepted=200,
            timeout=timeout,
        )
        return self._parse_model(HarnessProbeProjection, response)

    async def get_harness_models(
        self,
        harness_id: UUID,
        *,
        timeout: _Timeout = _UNSET,
    ) -> tuple[HarnessModelInfo, ...]:
        response = await self._request(
            "GET",
            f"harnesses/{harness_id}/models",
            accepted=200,
            timeout=timeout,
        )
        return _HARNESS_MODELS.validate_json(response.content)

    async def get_harness_modes(
        self,
        harness_id: UUID,
        *,
        timeout: _Timeout = _UNSET,
    ) -> tuple[HarnessModeInfo, ...]:
        response = await self._request(
            "GET",
            f"harnesses/{harness_id}/modes",
            accepted=200,
            timeout=timeout,
        )
        return _HARNESS_MODES.validate_json(response.content)

    # ------------------------------------------------------------------
    # Conversations and transcripts
    # ------------------------------------------------------------------

    async def list_conversations(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        include_archived: bool = True,
        timeout: _Timeout = _UNSET,
    ) -> Page[ConversationShell]:
        response = await self._request(
            "GET",
            "conversations",
            accepted=200,
            params={
                "cursor": cursor,
                "limit": limit,
                "include_archived": include_archived,
            },
            timeout=timeout,
        )
        return _PAGE_CONVERSATION.validate_json(response.content)

    async def create_conversation(
        self,
        harness_id: UUID,
        *,
        title: str | None = None,
        timeout: _Timeout = _UNSET,
    ) -> ConversationSnapshot:
        body: dict[str, Any] = {"harness_id": str(harness_id)}
        if title is not None:
            body["title"] = title
        response = await self._request(
            "POST",
            "conversations",
            accepted=201,
            json=body,
            timeout=timeout,
        )
        return self._parse_model(ConversationSnapshot, response)

    async def import_transcript(
        self,
        harness_id: UUID,
        document: TranscriptDocument,
        *,
        timeout: _Timeout = _UNSET,
    ) -> ConversationSnapshot:
        response = await self._request(
            "POST",
            "conversations/import",
            accepted=201,
            json={
                "harness_id": str(harness_id),
                "document": document.model_dump(mode="json"),
            },
            timeout=timeout,
        )
        return self._parse_model(ConversationSnapshot, response)

    async def search_conversations(
        self,
        query: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
        timeout: _Timeout = _UNSET,
    ) -> Page[ConversationSearchHit]:
        response = await self._request(
            "GET",
            "conversations/search",
            accepted=200,
            params={"q": query, "cursor": cursor, "limit": limit},
            timeout=timeout,
        )
        return _PAGE_SEARCH.validate_json(response.content)

    async def get_conversation(
        self,
        conversation_id: UUID,
        *,
        timeout: _Timeout = _UNSET,
    ) -> ConversationSnapshot:
        response = await self._request(
            "GET",
            f"conversations/{conversation_id}",
            accepted=200,
            timeout=timeout,
        )
        return self._parse_model(ConversationSnapshot, response)

    async def archive_conversation(
        self,
        conversation_id: UUID,
        *,
        timeout: _Timeout = _UNSET,
    ) -> ConversationSnapshot:
        response = await self._request(
            "POST",
            f"conversations/{conversation_id}/archive",
            accepted=200,
            timeout=timeout,
        )
        return self._parse_model(ConversationSnapshot, response)

    async def unarchive_conversation(
        self,
        conversation_id: UUID,
        *,
        timeout: _Timeout = _UNSET,
    ) -> ConversationSnapshot:
        response = await self._request(
            "POST",
            f"conversations/{conversation_id}/unarchive",
            accepted=200,
            timeout=timeout,
        )
        return self._parse_model(ConversationSnapshot, response)

    async def pin_conversation(
        self,
        conversation_id: UUID,
        *,
        timeout: _Timeout = _UNSET,
    ) -> ConversationSnapshot:
        response = await self._request(
            "POST",
            f"conversations/{conversation_id}/pin",
            accepted=200,
            timeout=timeout,
        )
        return self._parse_model(ConversationSnapshot, response)

    async def unpin_conversation(
        self,
        conversation_id: UUID,
        *,
        timeout: _Timeout = _UNSET,
    ) -> ConversationSnapshot:
        response = await self._request(
            "POST",
            f"conversations/{conversation_id}/unpin",
            accepted=200,
            timeout=timeout,
        )
        return self._parse_model(ConversationSnapshot, response)

    async def snooze_conversation(
        self,
        conversation_id: UUID,
        *,
        until: datetime,
        timeout: _Timeout = _UNSET,
    ) -> ConversationSnapshot:
        response = await self._request(
            "POST",
            f"conversations/{conversation_id}/snooze",
            accepted=200,
            json={"until": until.isoformat()},
            timeout=timeout,
        )
        return self._parse_model(ConversationSnapshot, response)

    async def unsnooze_conversation(
        self,
        conversation_id: UUID,
        *,
        timeout: _Timeout = _UNSET,
    ) -> ConversationSnapshot:
        response = await self._request(
            "POST",
            f"conversations/{conversation_id}/unsnooze",
            accepted=200,
            timeout=timeout,
        )
        return self._parse_model(ConversationSnapshot, response)

    async def delete_conversation(
        self,
        conversation_id: UUID,
        *,
        timeout: _Timeout = _UNSET,
    ) -> None:
        await self._request(
            "DELETE",
            f"conversations/{conversation_id}",
            accepted=204,
            timeout=timeout,
        )

    async def set_retention_exemption(
        self,
        conversation_id: UUID,
        *,
        exempt: bool,
        timeout: _Timeout = _UNSET,
    ) -> ConversationSnapshot:
        response = await self._request(
            "PUT",
            f"conversations/{conversation_id}/retention-exemption",
            accepted=200,
            json={"exempt": exempt},
            timeout=timeout,
        )
        return self._parse_model(ConversationSnapshot, response)

    async def export_transcript(
        self,
        conversation_id: UUID,
        *,
        timeout: _Timeout = _UNSET,
    ) -> TranscriptDocument:
        response = await self._request(
            "GET",
            f"conversations/{conversation_id}/transcript",
            accepted=200,
            timeout=timeout,
        )
        return self._parse_model(TranscriptDocument, response)

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    async def get_retention_policy(
        self,
        *,
        timeout: _Timeout = _UNSET,
    ) -> RetentionPolicyProjection:
        response = await self._request("GET", "retention", accepted=200, timeout=timeout)
        return self._parse_model(RetentionPolicyProjection, response)

    async def replace_retention_policy(
        self,
        months: int,
        *,
        timeout: _Timeout = _UNSET,
    ) -> RetentionPolicyProjection:
        response = await self._request(
            "PUT",
            "retention",
            accepted=200,
            json={"months": months},
            timeout=timeout,
        )
        return self._parse_model(RetentionPolicyProjection, response)

    async def preview_retention(
        self,
        *,
        timeout: _Timeout = _UNSET,
    ) -> RetentionPreviewProjection:
        response = await self._request("GET", "retention/preview", accepted=200, timeout=timeout)
        return self._parse_model(RetentionPreviewProjection, response)

    # ------------------------------------------------------------------
    # History pages
    # ------------------------------------------------------------------

    async def page_turns(
        self,
        conversation_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 50,
        timeout: _Timeout = _UNSET,
    ) -> Page[TurnProjection]:
        response = await self._request(
            "GET",
            f"conversations/{conversation_id}/turns",
            accepted=200,
            params={"cursor": cursor, "limit": limit},
            timeout=timeout,
        )
        return _PAGE_TURN.validate_json(response.content)

    async def page_messages(
        self,
        conversation_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 50,
        timeout: _Timeout = _UNSET,
    ) -> Page[MessageProjection]:
        response = await self._request(
            "GET",
            f"conversations/{conversation_id}/messages",
            accepted=200,
            params={"cursor": cursor, "limit": limit},
            timeout=timeout,
        )
        return _PAGE_MESSAGE.validate_json(response.content)

    async def page_tools(
        self,
        conversation_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 50,
        timeout: _Timeout = _UNSET,
    ) -> Page[ToolProjection]:
        response = await self._request(
            "GET",
            f"conversations/{conversation_id}/tools",
            accepted=200,
            params={"cursor": cursor, "limit": limit},
            timeout=timeout,
        )
        return _PAGE_TOOL.validate_json(response.content)

    async def page_plans(
        self,
        conversation_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 50,
        timeout: _Timeout = _UNSET,
    ) -> Page[PlanProjection]:
        response = await self._request(
            "GET",
            f"conversations/{conversation_id}/plans",
            accepted=200,
            params={"cursor": cursor, "limit": limit},
            timeout=timeout,
        )
        return _PAGE_PLAN.validate_json(response.content)

    async def page_activity(
        self,
        conversation_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 50,
        timeout: _Timeout = _UNSET,
    ) -> Page[ActivityProjection]:
        response = await self._request(
            "GET",
            f"conversations/{conversation_id}/activity",
            accepted=200,
            params={"cursor": cursor, "limit": limit},
            timeout=timeout,
        )
        return _PAGE_ACTIVITY.validate_json(response.content)

    # ------------------------------------------------------------------
    # Turn and conversation control
    # ------------------------------------------------------------------

    async def submit_turn(
        self,
        conversation_id: UUID,
        *,
        prompt: str,
        idempotency_key: str,
        model: str | None = None,
        timeout: _Timeout = _UNSET,
    ) -> SubmitTurnResult:
        body: dict[str, Any] = {"prompt": prompt}
        if model is not None:
            body["model"] = model
        response = await self._request(
            "POST",
            f"conversations/{conversation_id}/turns",
            accepted=202,
            json=body,
            headers={"Idempotency-Key": idempotency_key},
            timeout=timeout,
        )
        return self._parse_model(SubmitTurnResult, response)

    async def edit_queued_prompt(
        self,
        conversation_id: UUID,
        *,
        prompt: str,
        timeout: _Timeout = _UNSET,
    ) -> ConversationSnapshot:
        response = await self._request(
            "PATCH",
            f"conversations/{conversation_id}/queued-prompt",
            accepted=200,
            json={"prompt": prompt},
            timeout=timeout,
        )
        return self._parse_model(ConversationSnapshot, response)

    async def cancel_queued_prompt(
        self,
        conversation_id: UUID,
        *,
        timeout: _Timeout = _UNSET,
    ) -> CommandProjection | None:
        response = await self._request(
            "DELETE",
            f"conversations/{conversation_id}/queued-prompt",
            accepted=(200, 204),
            timeout=timeout,
        )
        if response.status_code == 204:
            return None
        return self._parse_model(CommandProjection, response)

    async def steer(
        self,
        conversation_id: UUID,
        *,
        prompt: str,
        idempotency_key: str,
        timeout: _Timeout = _UNSET,
    ) -> CommandProjection:
        response = await self._request(
            "POST",
            f"conversations/{conversation_id}/steer",
            accepted=202,
            json={"prompt": prompt},
            headers={"Idempotency-Key": idempotency_key},
            timeout=timeout,
        )
        return self._parse_model(CommandProjection, response)

    async def switch_harness(
        self,
        conversation_id: UUID,
        *,
        harness_id: UUID,
        idempotency_key: str,
        timeout: _Timeout = _UNSET,
    ) -> CommandProjection:
        response = await self._request(
            "POST",
            f"conversations/{conversation_id}/switch",
            accepted=202,
            json={"harness_id": str(harness_id)},
            headers={"Idempotency-Key": idempotency_key},
            timeout=timeout,
        )
        return self._parse_model(CommandProjection, response)

    async def interrupt(
        self,
        conversation_id: UUID,
        *,
        timeout: _Timeout = _UNSET,
    ) -> CommandProjection:
        response = await self._request(
            "POST",
            f"conversations/{conversation_id}/interrupt",
            accepted=202,
            timeout=timeout,
        )
        return self._parse_model(CommandProjection, response)

    # ------------------------------------------------------------------
    # Interactions
    # ------------------------------------------------------------------

    async def list_interactions(
        self,
        conversation_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 50,
        timeout: _Timeout = _UNSET,
    ) -> Page[InteractionProjection]:
        response = await self._request(
            "GET",
            f"conversations/{conversation_id}/interactions",
            accepted=200,
            params={"cursor": cursor, "limit": limit},
            timeout=timeout,
        )
        return _PAGE_INTERACTION.validate_json(response.content)

    async def update_interaction_draft(
        self,
        conversation_id: UUID,
        interaction_id: UUID,
        *,
        draft: dict[str, Any],
        timeout: _Timeout = _UNSET,
    ) -> InteractionProjection:
        response = await self._request(
            "PATCH",
            f"conversations/{conversation_id}/interactions/{interaction_id}/draft",
            accepted=200,
            json={"draft": draft},
            timeout=timeout,
        )
        return self._parse_model(InteractionProjection, response)

    async def resolve_interaction(
        self,
        conversation_id: UUID,
        interaction_id: UUID,
        *,
        decision: ApprovalDecision | None = None,
        answers: dict[str, Any] | None = None,
        create_rule: ApprovalRuleInput | None = None,
        timeout: _Timeout = _UNSET,
    ) -> CommandProjection:
        body: dict[str, Any] = {}
        if decision is not None:
            body["decision"] = decision.value
        if answers is not None:
            body["answers"] = answers
        if create_rule is not None:
            body["create_rule"] = create_rule.model_dump(mode="json")
        response = await self._request(
            "POST",
            f"conversations/{conversation_id}/interactions/{interaction_id}/resolve",
            accepted=202,
            json=body,
            timeout=timeout,
        )
        return self._parse_model(CommandProjection, response)

    # ------------------------------------------------------------------
    # Approval rules and interaction audits
    # ------------------------------------------------------------------

    async def list_approval_rules(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        timeout: _Timeout = _UNSET,
    ) -> Page[ApprovalRuleProjection]:
        response = await self._request(
            "GET",
            "approval-rules",
            accepted=200,
            params={"cursor": cursor, "limit": limit},
            timeout=timeout,
        )
        return _PAGE_APPROVAL_RULE.validate_json(response.content)

    async def create_approval_rule(
        self,
        rule: ApprovalRuleInput,
        *,
        timeout: _Timeout = _UNSET,
    ) -> ApprovalRuleProjection:
        response = await self._request(
            "POST",
            "approval-rules",
            accepted=201,
            json=rule.model_dump(mode="json"),
            timeout=timeout,
        )
        return self._parse_model(ApprovalRuleProjection, response)

    async def get_approval_rule(
        self,
        rule_id: UUID,
        *,
        timeout: _Timeout = _UNSET,
    ) -> ApprovalRuleProjection:
        response = await self._request(
            "GET",
            f"approval-rules/{rule_id}",
            accepted=200,
            timeout=timeout,
        )
        return self._parse_model(ApprovalRuleProjection, response)

    async def replace_approval_rule(
        self,
        rule_id: UUID,
        rule: ApprovalRuleInput,
        *,
        timeout: _Timeout = _UNSET,
    ) -> ApprovalRuleProjection:
        response = await self._request(
            "PUT",
            f"approval-rules/{rule_id}",
            accepted=200,
            json=rule.model_dump(mode="json"),
            timeout=timeout,
        )
        return self._parse_model(ApprovalRuleProjection, response)

    async def delete_approval_rule(self, rule_id: UUID, *, timeout: _Timeout = _UNSET) -> None:
        await self._request(
            "DELETE",
            f"approval-rules/{rule_id}",
            accepted=204,
            timeout=timeout,
        )

    async def list_interaction_audits(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        timeout: _Timeout = _UNSET,
    ) -> Page[InteractionAuditProjection]:
        response = await self._request(
            "GET",
            "interaction-audits",
            accepted=200,
            params={"cursor": cursor, "limit": limit},
            timeout=timeout,
        )
        return _PAGE_INTERACTION_AUDIT.validate_json(response.content)

    async def get_interaction_audit(
        self,
        audit_id: UUID,
        *,
        timeout: _Timeout = _UNSET,
    ) -> InteractionAuditProjection:
        response = await self._request(
            "GET",
            f"interaction-audits/{audit_id}",
            accepted=200,
            timeout=timeout,
        )
        return self._parse_model(InteractionAuditProjection, response)

    # ------------------------------------------------------------------
    # SSE event streaming
    # ------------------------------------------------------------------

    def stream_conversation_events(
        self,
        conversation_id: UUID,
        *,
        after_sequence: int = 0,
        timeout: _Timeout = _UNSET,
    ) -> AsyncIterator[ConversationStreamItem]:
        return self._stream_conversation_events(
            conversation_id,
            after_sequence=after_sequence,
            timeout=timeout,
        )

    async def _stream_conversation_events(
        self,
        conversation_id: UUID,
        *,
        after_sequence: int = 0,
        timeout: _Timeout = _UNSET,
    ) -> AsyncGenerator[ConversationStreamItem, None]:
        cursor = after_sequence
        next_delay = _BACKOFF_INITIAL_S
        first_attempt = True
        path = f"conversations/{conversation_id}/events"

        while True:
            if not first_attempt:
                await asyncio.sleep(next_delay)
                next_delay = min(next_delay * 2, _BACKOFF_CAP_S)

            headers = self._request_headers()
            if not first_attempt or cursor != 0:
                headers["Last-Event-ID"] = str(cursor)

            effective = self._resolved_timeout(timeout)
            if effective is None:
                stream_timeout: httpx.Timeout | None = httpx.Timeout(None)
            else:
                stream_timeout = httpx.Timeout(effective, read=None)

            try:
                async with self._client.stream(
                    "GET",
                    path,
                    headers=headers,
                    timeout=stream_timeout,
                ) as response:
                    if response.status_code != 200:
                        await response.aread()
                        raise APIError.from_response(response)

                    content_type = response.headers.get("content-type", "")
                    if not content_type.startswith("text/event-stream"):
                        await response.aread()
                        raise ValueError(
                            f"expected text/event-stream content type, got {content_type!r}"
                        )

                    decoder = SseDecoder(max_partial_bytes=None)
                    try:
                        async for chunk in response.aiter_bytes():
                            for frame in decoder.feed(chunk):
                                item = self._parse_stream_frame(
                                    frame.event,
                                    frame.id,
                                    frame.data,
                                )
                                cursor = item.sequence
                                next_delay = _BACKOFF_INITIAL_S
                                yield item
                                if self._is_terminal_deletion(item):
                                    return
                    except httpx.TransportError:
                        first_attempt = False
                        continue
            except httpx.TransportError:
                first_attempt = False
                continue

            # Clean EOF without a terminal deletion: reconnect.
            first_attempt = False

    @staticmethod
    def _parse_stream_frame(
        event_name: str | None,
        event_id: str | None,
        data: str,
    ) -> ConversationStreamItem:
        if event_name is None or event_id is None or not data:
            raise ValueError("SSE frame missing event, id, or data")
        try:
            sequence = int(event_id)
        except ValueError as exc:
            raise ValueError(f"SSE id is not an integer: {event_id!r}") from exc

        if event_name == "snapshot":
            item: ConversationStreamItem = ConversationSnapshot.model_validate_json(data)
        elif event_name == "sync":
            item = SyncProjection.model_validate_json(data)
        else:
            event = conversation_event_adapter.validate_json(data)
            if event.type != event_name:
                raise ValueError(
                    f"SSE event name {event_name!r} does not match ConversationEvent.type "
                    f"{event.type!r}"
                )
            item = event

        if item.sequence != sequence:
            raise ValueError(f"SSE id {sequence} does not match payload sequence {item.sequence}")
        return item

    @staticmethod
    def _is_terminal_deletion(item: ConversationStreamItem) -> bool:
        if not isinstance(item, ConversationEvent):
            return False
        payload = item.payload
        return (
            isinstance(payload, ConversationMetadataChangedPayload)
            and payload.deleted_at is not None
        )
