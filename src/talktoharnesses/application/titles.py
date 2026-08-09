"""Pure derived-title helper shared by projection materialization and retention."""

from __future__ import annotations

_MAX_TITLE_WORDS = 8


def derive_title_from_user_message(text: str) -> str | None:
    """Derive a title from the first eight whitespace-delimited words.

    Collapses internal whitespace and returns ``None`` for empty/whitespace-only
    text. Callers store the result as ``Conversation.title_derived``; precedence
    over native/manual titles is expressed only by ``Conversation.display_title``.
    """
    words = text.split()
    if not words:
        return None
    return " ".join(words[:_MAX_TITLE_WORDS])
