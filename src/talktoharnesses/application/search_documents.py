"""Portable sanitized search-document builder (shared SQLite/PG backend)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from talktoharnesses.application.redaction import StreamingTextRedactor
from talktoharnesses.domain.models import CanonicalToolResult, Message


@dataclass(frozen=True, slots=True)
class SearchDocumentFields:
    """Four derived fields written to ``SearchDocument`` together."""

    search_title: str
    search_body: str
    snippet_text: str
    normalized_text: str


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


def _collect_body_parts(
    *,
    message_texts: Iterable[str],
    tool_names: Iterable[str],
    tool_arguments: Iterable[dict[str, Any]],
    tool_paths: Iterable[str],
    tool_output_tails: Iterable[str],
    redaction_patterns: Sequence[str],
) -> tuple[list[str], list[str]]:
    """Return (display_parts, normalized_source_parts) for body content."""
    display: list[str] = []
    for text in message_texts:
        if text:
            display.append(_redact(text, redaction_patterns))
    for name in tool_names:
        if name:
            display.append(name)
    for args in tool_arguments:
        if args:
            display.append(_normalize_arguments(args))
    for path in tool_paths:
        if path:
            display.append(path)
    for tail in tool_output_tails:
        if tail:
            display.append(_redact(tail, redaction_patterns))
    return display, display


def build_search_document(
    *,
    title: str,
    messages: Iterable[Message] = (),
    tools: Iterable[CanonicalToolResult] = (),
    redaction_patterns: Sequence[str] = (),
) -> SearchDocumentFields:
    """Build derived search fields for one conversation.

    Includes:
    - effective conversation title
    - user and assistant message text
    - tool name, normalized arguments, paths, and 2 KiB output tail

    Excludes reasoning, raw/native events, stderr, secrets (via redactor), and
    full raw tool output.
    """
    return build_search_document_from_parts(
        title=title,
        message_texts=(message.text for message in messages if message.text),
        tool_names=(tool.tool_name for tool in tools),
        tool_arguments=(tool.arguments for tool in tools if tool.arguments),
        tool_paths=(path for tool in tools for path in tool.paths),
        tool_output_tails=(tool.output_tail for tool in tools if tool.output_tail),
        redaction_patterns=redaction_patterns,
    )


def build_search_document_from_parts(
    *,
    title: str,
    message_texts: Iterable[str] = (),
    tool_names: Iterable[str] = (),
    tool_arguments: Iterable[dict[str, Any]] = (),
    tool_paths: Iterable[str] = (),
    tool_output_tails: Iterable[str] = (),
    redaction_patterns: Sequence[str] = (),
) -> SearchDocumentFields:
    """Builder entry used by projection materializers without full domain models."""
    redacted_title = _redact(title, redaction_patterns) if title else ""
    search_title = _normalize(redacted_title) if redacted_title else ""
    display_parts, _ = _collect_body_parts(
        message_texts=message_texts,
        tool_names=tool_names,
        tool_arguments=tool_arguments,
        tool_paths=tool_paths,
        tool_output_tails=tool_output_tails,
        redaction_patterns=redaction_patterns,
    )
    snippet_text = " ".join(p for p in display_parts if p)
    search_body = _normalize(snippet_text) if snippet_text else ""
    if search_title and search_body:
        normalized_text = f"{search_title} {search_body}"
    else:
        normalized_text = search_title or search_body
    return SearchDocumentFields(
        search_title=search_title,
        search_body=search_body,
        snippet_text=snippet_text,
        normalized_text=normalized_text,
    )


def _normalize_arguments(arguments: dict[str, Any]) -> str:
    try:
        return json.dumps(arguments, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(arguments)
