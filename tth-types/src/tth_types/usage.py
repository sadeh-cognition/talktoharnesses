"""The turn-usage accumulator that produces the canonical usage payload.

A ``usage_updated`` payload reports the turn's totals so far, never the
increment since the previous payload, so a consumer replaces what the same turn
reported before it rather than adding successive payloads together. Providers
report on their own terms: some send what each request spent, some send the
turn's own figures, some send both. Turning either shape into the one canonical
meaning is the same machine every time, so it lives here, beside the payload it
produces, and each adapter keeps only its wire-shape mapping.
"""

from __future__ import annotations

from typing import TypedDict
from uuid import UUID

from tth_types.events import UsageUpdatedPayload

__all__ = ["TurnUsage", "UsageFields"]


class UsageFields(TypedDict, total=False):
    """The canonical usage fields, for an adapter that assembles them before reporting."""

    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cached_input_tokens: int | None


def _counted(value: object) -> int | None:
    """The value if it is a token count, else ``None``.

    Every reported token value must be a nonnegative integer. ``type`` is
    compared exactly so ``True`` is not counted as ``1``.
    """
    if type(value) is int and value >= 0:
        return value
    return None


def _counted_fields(
    input_tokens: object,
    output_tokens: object,
    total_tokens: object,
    cached_input_tokens: object,
) -> dict[str, int]:
    """The canonical fields that carry a token count, by payload field name."""
    fields = {
        "input_tokens": _counted(input_tokens),
        "output_tokens": _counted(output_tokens),
        "total_tokens": _counted(total_tokens),
        "cached_input_tokens": _counted(cached_input_tokens),
    }
    return {name: value for name, value in fields.items() if value is not None}


class TurnUsage:
    """Accumulate a provider's usage frames into one turn's totals.

    ``add`` takes what a single request spent and sums it; ``replace`` takes
    figures that are already the turn's own and supersedes the running total
    with them. A field is reported only when every frame that contributed
    supplied it, so a partial sum is left absent rather than understating the
    turn. Nothing here derives a category from another: a total the provider
    never sent stays absent.
    """

    def __init__(self) -> None:
        self._totals: dict[str, int] = {}
        self._counts: dict[str, int] = {}
        self._frames = 0
        self._frame_ids: set[str] = set()
        self._final = False

    def reset(self) -> None:
        """Forget the previous turn. Call from the adapter's ``begin_turn``."""
        self._totals = {}
        self._counts = {}
        self._frames = 0
        self._frame_ids = set()
        self._final = False

    @property
    def reported(self) -> bool:
        """Whether any frame has contributed a token count to this turn."""
        return bool(self._totals)

    def add(
        self,
        turn_id: UUID | None,
        *,
        frame_id: str | None = None,
        input_tokens: object = None,
        output_tokens: object = None,
        total_tokens: object = None,
        cached_input_tokens: object = None,
    ) -> list[UsageUpdatedPayload]:
        """Add one request's usage and report the turn's totals so far.

        ``frame_id`` identifies the provider frame the figures came from. A
        frame may be re-delivered — a replayed stream, a retried read, a
        reconnect that repeats history — and the same request must not be
        counted twice.
        """
        if self._final:
            # The turn already reported its own authoritative figures. A frame
            # trailing them would restart the running total and report a
            # smaller one, which is the opposite of superseding it.
            return []
        if frame_id:
            if frame_id in self._frame_ids:
                return []
            self._frame_ids.add(frame_id)
        counted = _counted_fields(input_tokens, output_tokens, total_tokens, cached_input_tokens)
        if not counted:
            return []
        self._frames += 1
        for name, value in counted.items():
            self._totals[name] = self._totals.get(name, 0) + value
            self._counts[name] = self._counts.get(name, 0) + 1
        return self._payloads(turn_id)

    def replace(
        self,
        turn_id: UUID | None,
        *,
        final: bool = True,
        input_tokens: object = None,
        output_tokens: object = None,
        total_tokens: object = None,
        cached_input_tokens: object = None,
    ) -> list[UsageUpdatedPayload]:
        """Report figures that are already the turn's totals.

        These supersede whatever the turn reported before. ``final`` closes the
        turn to further frames, which is what a provider's terminal usage
        means; a provider that keeps sending its own running totals passes
        ``final=False``. A running total arriving after the terminal one is
        ignored like any other trailing frame.
        """
        if self._final and not final:
            return []
        counted = _counted_fields(input_tokens, output_tokens, total_tokens, cached_input_tokens)
        if not counted:
            return []
        self._totals = counted
        self._counts = dict.fromkeys(counted, 1)
        self._frames = 1
        self._final = final
        return self._payloads(turn_id)

    def report(self, turn_id: UUID | None) -> list[UsageUpdatedPayload]:
        """The turn's totals so far, for a provider that reports only at the end.

        Frames are still added as they arrive so the figures are ready, but
        nothing is published until the adapter asks.
        """
        return self._payloads(turn_id)

    def _payloads(self, turn_id: UUID | None) -> list[UsageUpdatedPayload]:
        reported = {
            name: value
            for name, value in self._totals.items()
            if self._counts.get(name) == self._frames
        }
        if not reported:
            return []
        return [
            UsageUpdatedPayload(
                turn_id=turn_id,
                input_tokens=reported.get("input_tokens"),
                output_tokens=reported.get("output_tokens"),
                total_tokens=reported.get("total_tokens"),
                cached_input_tokens=reported.get("cached_input_tokens"),
            )
        ]
