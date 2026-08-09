"""Portable search-document builder."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from talktoharnesses.application.search_documents import (
    build_search_document,
    build_search_document_from_parts,
    normalize_search_terms,
)
from talktoharnesses.domain.enums import MessageRole, ToolOutcome
from talktoharnesses.domain.models import CanonicalToolResult, Message


def test_includes_title_messages_and_tool_tail() -> None:
    now = datetime(2026, 8, 8, tzinfo=UTC)
    turn_id = uuid4()
    messages = (
        Message(turn_id=turn_id, role=MessageRole.USER, text="Hello World", created_at=now),
        Message(turn_id=turn_id, role=MessageRole.ASSISTANT, text="Hi there", created_at=now),
    )
    tools = (
        CanonicalToolResult(
            turn_id=turn_id,
            tool_name="read_file",
            arguments={"path": "/tmp/a"},
            outcome=ToolOutcome.SUCCESS,
            paths=("/tmp/a",),
            output_tail="file contents",
            full_output="SHOULD_NOT_APPEAR_IN_SEARCH",
        ),
    )
    doc = build_search_document(title="My Chat", messages=messages, tools=tools)
    assert "my chat" in doc
    assert "hello world" in doc
    assert "hi there" in doc
    # Non-alphanumeric characters (underscore, slash) become spaces.
    assert "read file" in doc
    assert "tmp a" in doc
    assert "file contents" in doc
    assert "should not appear in search" not in doc


def test_redaction_patterns() -> None:
    doc = build_search_document_from_parts(
        title="secret sk-abc123 here",
        message_texts=["token sk-abc123"],
        redaction_patterns=("sk-abc123",),
    )
    assert "sk-abc123" not in doc
    # "[REDACTED]" loses its brackets under the shared alphanumeric normalizer.
    assert "redacted" in doc


def test_normalize_search_terms_casefolds_and_splits_on_punctuation() -> None:
    assert normalize_search_terms("Hello, World!  read_file /tmp/a") == (
        "hello",
        "world",
        "read",
        "file",
        "tmp",
        "a",
    )


def test_normalize_search_terms_empty_input() -> None:
    assert normalize_search_terms("   !!! ,,, ") == ()


def test_document_and_query_share_the_same_term_stream() -> None:
    doc = build_search_document(title="Read /tmp/a-file.txt now")
    assert " ".join(normalize_search_terms("tmp a file txt")) in doc
