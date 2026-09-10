"""Grok ACP and xAI-extension event normalization."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from tth_types.enums import ErrorCode
from tth_types.errors import DomainError
from tth_types.events import CostUpdatedPayload, HarnessEvent

from tth_grok.acp.normalizer import AcpSessionNormalizer

logger = logging.getLogger(__name__)

_COST_TICKS_PER_USD = Decimal(10_000_000_000)


class GrokNormalizer(AcpSessionNormalizer):
    """Add Grok's live and terminal xAI usage notifications to ACP normalization."""

    def on_xai_session_notification(self, params: dict[str, Any]) -> list[HarnessEvent]:
        self._validate_session(params, "_x.ai/session_notification")
        update = _as_dict(params.get("update"))
        if update is None:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                "_x.ai/session_notification missing update object",
            )
        kind = update.get("sessionUpdate")
        if kind == "response_completed":
            return self._response_completed(_as_dict(update.get("usage")))
        if kind != "turn_completed":
            # The envelope is unvalidated and multiplexes kinds this adapter
            # does not model, so nothing here may raise. A new kind carrying
            # usage is worth finding rather than dropping in silence.
            if "usage" in update:
                logger.info(
                    "Grok %s notification carries unmapped usage %s",
                    kind,
                    _as_dict(update.get("usage")),
                )
            else:
                logger.debug("ignoring Grok session notification %s", kind)
            return []
        usage = _as_dict(update.get("usage"))
        if usage is None or self._active_turn_id is None:
            return []
        return self._turn_completed(self._active_turn_id, usage)

    def _response_completed(self, usage: dict[str, Any] | None) -> list[HarnessEvent]:
        """Report the turn's totals so far from one response's own usage.

        Grok reports each response separately while a turn runs, counting
        fresh input apart from cache reads, where the terminal frame counts
        input cache-inclusive. Adding cache reads back in keeps every report a
        turn makes on one scale, so the figures climb instead of jumping when
        the turn ends. These frames carry no total, so the running reports omit
        that category until the terminal frame supplies it.
        """
        if usage is None or self._active_turn_id is None:
            return []
        fresh_input = _optional_int(usage.get("input_tokens"))
        cached = _optional_int(usage.get("cache_read_input_tokens"))
        cache_inclusive_input = (
            None if fresh_input is None and cached is None else (fresh_input or 0) + (cached or 0)
        )
        return list(
            self._usage.add(
                self._active_turn_id,
                input_tokens=cache_inclusive_input,
                output_tokens=usage.get("output_tokens"),
                cached_input_tokens=cached,
            )
        )

    def _turn_completed(self, turn_id: UUID, usage: dict[str, Any]) -> list[HarnessEvent]:
        """Report the turn's own figures, which supersede its responses'.

        They also close the turn: a response frame trailing the terminal one
        would otherwise restart the running total and report a smaller one.
        """
        events: list[HarnessEvent] = list(
            self._usage.replace(
                turn_id,
                input_tokens=usage.get("inputTokens"),
                output_tokens=usage.get("outputTokens"),
                total_tokens=usage.get("totalTokens"),
                cached_input_tokens=usage.get("cachedReadTokens"),
            )
        )
        cost = _cost_from_ticks(usage.get("costUsdTicks"))
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
