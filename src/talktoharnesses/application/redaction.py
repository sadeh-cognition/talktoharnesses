"""Centralized streaming text redaction for stderr and persistence boundaries."""

from __future__ import annotations

from collections.abc import Sequence

_REDACTED = "[REDACTED]"


class StreamingTextRedactor:
    """Incremental redactor that handles secrets split across feed() chunks.

    Patterns are replaced with ``[REDACTED]``. A carry buffer of
    ``max(len(pattern)) - 1`` bytes is retained so multi-chunk secrets cannot
    leak. Call ``flush()`` at end-of-stream to emit the remaining carry.
    """

    def __init__(self, patterns: Sequence[str] = ()) -> None:
        # Longest first so longer secrets win over substrings.
        self._patterns: tuple[str, ...] = tuple(
            sorted((p for p in patterns if p), key=len, reverse=True)
        )
        self._max_partial = max((len(p) for p in self._patterns), default=0)
        self._carry = ""

    def feed(self, text: str) -> str:
        if not text and not self._carry:
            return ""
        combined = self._carry + text
        if not self._patterns:
            self._carry = ""
            return combined
        redacted = combined
        for pattern in self._patterns:
            redacted = redacted.replace(pattern, _REDACTED)
        # Keep a suffix that could be a prefix of a secret in the next chunk.
        hold = max(0, self._max_partial - 1)
        if hold == 0:
            self._carry = ""
            return redacted
        if len(redacted) <= hold:
            self._carry = redacted
            return ""
        emit, self._carry = redacted[:-hold], redacted[-hold:]
        return emit

    def flush(self) -> str:
        remaining = self._carry
        self._carry = ""
        if not remaining:
            return ""
        if not self._patterns:
            return remaining
        redacted = remaining
        for pattern in self._patterns:
            redacted = redacted.replace(pattern, _REDACTED)
        return redacted
