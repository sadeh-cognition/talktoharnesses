"""Pure search query grammar and ranking."""

from __future__ import annotations

import pytest

from talktoharnesses.application.search_query import (
    BODY_PHRASE_POINTS,
    BODY_TERM_CAP,
    BODY_TERM_POINTS,
    TITLE_CLAUSE_POINTS,
    TITLE_TERM_POINTS,
    build_snippet,
    count_token_occurrences,
    escape_fts5_token,
    parse_search_query,
    rank_document,
)
from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError


def test_parse_terms_phrases_exclusions_and_filters() -> None:
    query = parse_search_query(
        'alpha "hello world" -beta -"skip me" is:pinned harness:codex after:2026-01-01'
    )
    assert [c.normalized for c in query.positive] == ["alpha", "hello world"]
    assert query.positive[1].is_phrase is True
    assert [c.normalized for c in query.exclusions] == ["beta", "skip me"]
    assert query.filters.pinned is True
    assert query.filters.harness is HarnessKind.CODEX
    assert query.filters.after is not None
    assert query.digest


def test_reject_empty_and_unknown() -> None:
    with pytest.raises(DomainError) as exc:
        parse_search_query("is:pinned")
    assert exc.value.code is ErrorCode.INVALID_SEARCH_QUERY
    with pytest.raises(DomainError) as exc2:
        parse_search_query("alpha foo:bar")
    assert exc2.value.code is ErrorCode.INVALID_SEARCH_QUERY
    with pytest.raises(DomainError) as exc3:
        parse_search_query('alpha "unterminated')
    assert exc3.value.code is ErrorCode.INVALID_SEARCH_QUERY


def test_rank_and_snippet() -> None:
    query = parse_search_query("alpha")
    score = rank_document(query, search_title="alpha notes", search_body="more alpha alpha")
    assert score == TITLE_CLAUSE_POINTS + TITLE_TERM_POINTS + BODY_TERM_POINTS * 2
    snippet = build_snippet(query, "prefix alpha suffix")
    assert snippet is not None
    assert "alpha" in snippet.text
    assert "alpha" in snippet.matched_terms
    assert build_snippet(query, "no match here") is None


def test_occurrence_caps_and_fts5_escape() -> None:
    assert count_token_occurrences("a a a a a", "a") == 5
    query = parse_search_query("a")
    score = rank_document(
        query,
        search_title="",
        search_body=" ".join(["a"] * 20),
    )
    assert score == BODY_TERM_POINTS * BODY_TERM_CAP
    assert escape_fts5_token('he"llo') == '"he""llo"'


def test_repeated_identical_filters_are_accepted() -> None:
    query = parse_search_query(
        "alpha is:pinned is:pinned is:archived is:archived has:interaction has:interaction"
    )
    assert query.filters.pinned is True
    assert query.filters.archived is True
    assert query.filters.has_interaction is True


def test_unsupported_phrase_escape_is_rejected() -> None:
    with pytest.raises(DomainError) as exc:
        parse_search_query(r'"foo\q"')
    assert exc.value.code is ErrorCode.INVALID_SEARCH_QUERY


def test_unquoted_punctuation_does_not_receive_phrase_points() -> None:
    term_score = rank_document(
        parse_search_query("foo-bar"), search_title="", search_body="foo bar"
    )
    phrase_score = rank_document(
        parse_search_query('"foo-bar"'), search_title="", search_body="foo bar"
    )
    assert phrase_score == term_score + BODY_PHRASE_POINTS


def test_snippet_ellipses_fit_inside_code_point_limit() -> None:
    snippet = build_snippet(parse_search_query("alpha"), "left " * 100 + "alpha" + " right" * 100)
    assert snippet is not None
    assert snippet.text.startswith("…")
    assert snippet.text.endswith("…")
    assert len(snippet.text) <= 240
