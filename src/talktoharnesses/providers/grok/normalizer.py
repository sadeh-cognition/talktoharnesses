"""Grok ACP and xAI-extension event normalization."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import CostUpdatedPayload, HarnessEvent, UsageUpdatedPayload
from talktoharnesses.providers.acp.normalizer import AcpSessionNormalizer

_COST_TICKS_PER_USD = Decimal(10_000_000_000)


class GrokNormalizer(AcpSessionNormalizer):
    """Add Grok's live xAI turn-usage notification to ACP normalization."""

    def on_xai_session_notification(self, params: dict[str, Any]) -> list[HarnessEvent]:
        self._validate_session(params, "_x.ai/session_notification")
        update = _as_dict(params.get("update"))
        if update is None:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                "_x.ai/session_notification missing update object",
            )
        if update.get("sessionUpdate") != "turn_completed":
            return []
        usage = _as_dict(update.get("usage"))
        if usage is None:
            return []
        return _usage_events(self._require_turn(), usage)


def _usage_events(turn_id: UUID, usage: dict[str, Any]) -> list[HarnessEvent]:
    input_tokens = _optional_int(usage.get("inputTokens"))
    output_tokens = _optional_int(usage.get("outputTokens"))
    total_tokens = _optional_int(usage.get("totalTokens"))
    cached_input_tokens = _optional_int(usage.get("cachedReadTokens"))
    cost = _cost_from_ticks(usage.get("costUsdTicks"))
    if all(
        value is None
        for value in (input_tokens, output_tokens, total_tokens, cached_input_tokens, cost)
    ):
        return []
    events: list[HarnessEvent] = [
        UsageUpdatedPayload(
            turn_id=turn_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cached_input_tokens=cached_input_tokens,
        )
    ]
    if cost is not None:
        events.append(CostUpdatedPayload(turn_id=turn_id, cost=cost, currency="USD"))
    return events


def _cost_from_ticks(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    amount = (Decimal(value) / _COST_TICKS_PER_USD).normalize()
    return format(amount, "f")


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_dict(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    raw = cast(dict[object, object], value)
    return {str(key): item for key, item in raw.items()}

__all__ = ["GrokNormalizer"]
