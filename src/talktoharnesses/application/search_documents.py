"""Portable sanitized search-document builder (shared SQLite/PG backend)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any

from talktoharnesses.application.redaction import StreamingTextRedactor
from talktoharnesses.domain.models import CanonicalToolResult, Message


def normalize_search_terms(text: str) -> tuple[str, ...]:
    """Case-fold, replace non-alphanumeric characters with spaces, and split.

    Shared by both document building and query normalization so a PostgreSQL
    or SQLite FTS backend can query the exact same term stream that built the
    stored document (see ``docs/phase8.md`` Work Package 4).
    """
    folded = text.casefold()
    stripped = "".join(char if char.isalnum() else " " for char in folded)
    return tuple(stripped.split())


def _normalize(text: str) -> str:
    return " ".join(normalize_search_terms(text))


def _redact(text: str, patterns: Sequence[str] = ()) -> str:
    if not patterns:
        return text
    redactor = StreamingTextRedactor(patterns)
    out = redactor.feed(text)
    out += redactor.flush()
    return out


def build_search_document(
    *,
    title: str,
    messages: Iterable[Message] = (),
    tools: Iterable[CanonicalToolResult] = (),
    redaction_patterns: Sequence[str] = (),
) -> str:
    """Build normalized search text for one conversation.

    Includes:
    - effective conversation title
    - user and assistant message text
    - tool name, normalized arguments, paths, and 2 KiB output tail

    Excludes reasoning, raw/native events, stderr, secrets (via redactor), and
    full raw tool output.
    """
    parts: list[str] = []
    if title:
        parts.append(_redact(title, redaction_patterns))
    for message in messages:
        if message.text:
            parts.append(_redact(message.text, redaction_patterns))
    for tool in tools:
        parts.append(tool.tool_name)
        if tool.arguments:
            parts.append(_normalize_arguments(tool.arguments))
        for path in tool.paths:
            parts.append(path)
        if tool.output_tail:
            parts.append(_redact(tool.output_tail, redaction_patterns))
    return _normalize(" ".join(p for p in parts if p))


def build_search_document_from_parts(
    *,
    title: str,
    message_texts: Iterable[str] = (),
    tool_names: Iterable[str] = (),
    tool_arguments: Iterable[dict[str, Any]] = (),
    tool_paths: Iterable[str] = (),
    tool_output_tails: Iterable[str] = (),
    redaction_patterns: Sequence[str] = (),
) -> str:
    """Builder entry used by projection materializers without full domain models."""
    parts: list[str] = []
    if title:
        parts.append(_redact(title, redaction_patterns))
    for text in message_texts:
        if text:
            parts.append(_redact(text, redaction_patterns))
    for name in tool_names:
        if name:
            parts.append(name)
    for args in tool_arguments:
        if args:
            parts.append(_normalize_arguments(args))
    for path in tool_paths:
        if path:
            parts.append(path)
    for tail in tool_output_tails:
        if tail:
            parts.append(_redact(tail, redaction_patterns))
    return _normalize(" ".join(p for p in parts if p))


def _normalize_arguments(arguments: dict[str, Any]) -> str:
    try:
        return json.dumps(arguments, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(arguments)
