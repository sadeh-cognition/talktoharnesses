"""Pure search query grammar, normalization, ranking constants, and snippets."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from talktoharnesses.application.search_documents import normalize_search_terms
from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import SearchSnippet

MAX_QUERY_CODE_POINTS = 512
MAX_TEXT_CLAUSES = 8

TITLE_CLAUSE_POINTS = 32
TITLE_TERM_POINTS = 8
TITLE_TERM_CAP = 4
BODY_PHRASE_POINTS = 4
BODY_PHRASE_CAP = 4
BODY_TERM_POINTS = 1
BODY_TERM_CAP = 8

SNIPPET_CODE_POINTS = 240

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_IS_VALUES = frozenset({"pinned", "archived"})
_HARNESS_VALUES = frozenset(kind.value for kind in HarnessKind)


@dataclass(frozen=True, slots=True)
class TextClause:
    """One positive or negative text clause (term or phrase)."""

    tokens: tuple[str, ...]
    is_phrase: bool
    negative: bool = False

    @property
    def normalized(self) -> str:
        return " ".join(self.tokens)


@dataclass(frozen=True, slots=True)
class SearchFilters:
    pinned: bool | None = None
    archived: bool | None = None
    has_interaction: bool | None = None
    harness: HarnessKind | None = None
    before: datetime | None = None
    after: datetime | None = None


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """Normalized, digest-stable parsed search query."""

    positive: tuple[TextClause, ...]
    exclusions: tuple[TextClause, ...]
    filters: SearchFilters
    digest: str
    raw: str


@dataclass(frozen=True, slots=True)
class _RawToken:
    kind: str  # "word" | "phrase" | "filter"
    value: str
    negative: bool = False
    filter_key: str = ""


def parse_search_query(raw: str) -> SearchQuery:
    """Parse ``q`` into a normalized query or raise ``invalid_search_query``."""
    if len(raw) > MAX_QUERY_CODE_POINTS:
        raise DomainError(ErrorCode.INVALID_SEARCH_QUERY, "search query too long")
    raw_tokens = _tokenize(raw)
    positive: list[TextClause] = []
    exclusions: list[TextClause] = []
    pinned: bool | None = None
    archived: bool | None = None
    has_interaction: bool | None = None
    harness: HarnessKind | None = None
    before: datetime | None = None
    after: datetime | None = None

    for token in raw_tokens:
        if token.kind == "filter":
            if token.negative:
                raise DomainError(ErrorCode.INVALID_SEARCH_QUERY, "filters cannot be negated")
            key = token.filter_key
            value = token.value
            if key == "is":
                value = value.casefold()
                if value not in _IS_VALUES:
                    raise DomainError(ErrorCode.INVALID_SEARCH_QUERY, "invalid is: filter")
                if value == "pinned":
                    pinned = True
                else:
                    archived = True
            elif key == "has":
                if value.casefold() != "interaction":
                    raise DomainError(ErrorCode.INVALID_SEARCH_QUERY, "invalid has: filter")
                has_interaction = True
            elif key == "harness":
                kind = value.casefold()
                if kind not in _HARNESS_VALUES:
                    raise DomainError(ErrorCode.INVALID_SEARCH_QUERY, "invalid harness:")
                parsed_kind = HarnessKind(kind)
                if harness is not None and harness != parsed_kind:
                    raise DomainError(ErrorCode.INVALID_SEARCH_QUERY, "conflicting harness:")
                harness = parsed_kind
            elif key == "before":
                bound = _parse_day(value)
                if before is not None and before != bound:
                    raise DomainError(ErrorCode.INVALID_SEARCH_QUERY, "conflicting before:")
                before = bound
            elif key == "after":
                bound = _parse_day(value)
                if after is not None and after != bound:
                    raise DomainError(ErrorCode.INVALID_SEARCH_QUERY, "conflicting after:")
                after = bound
            else:
                raise DomainError(ErrorCode.INVALID_SEARCH_QUERY, "unknown operator")
            continue

        tokens = normalize_search_terms(token.value)
        if not tokens:
            detail = "empty phrase" if token.kind == "phrase" else "empty term"
            raise DomainError(ErrorCode.INVALID_SEARCH_QUERY, detail)
        clause = TextClause(
            tokens=tokens,
            is_phrase=token.kind == "phrase",
            negative=token.negative,
        )
        if clause.negative:
            exclusions.append(clause)
        else:
            positive.append(clause)

    if len(positive) + len(exclusions) > MAX_TEXT_CLAUSES:
        raise DomainError(ErrorCode.INVALID_SEARCH_QUERY, "too many text clauses")
    if not positive:
        raise DomainError(
            ErrorCode.INVALID_SEARCH_QUERY,
            "at least one positive term or phrase required",
        )

    filters = SearchFilters(
        pinned=pinned,
        archived=archived,
        has_interaction=has_interaction,
        harness=harness,
        before=before,
        after=after,
    )
    return SearchQuery(
        positive=tuple(positive),
        exclusions=tuple(exclusions),
        filters=filters,
        digest=_digest(positive, exclusions, filters),
        raw=raw,
    )


def count_token_occurrences(haystack: str, needle: str) -> int:
    """Count complete space-delimited token or token-sequence occurrences."""
    if not needle or not haystack:
        return 0
    padded = f" {haystack} "
    target = f" {needle} "
    count = 0
    start = 0
    while True:
        index = padded.find(target, start)
        if index < 0:
            return count
        count += 1
        start = index + len(target) - 1


def rank_document(query: SearchQuery, *, search_title: str, search_body: str) -> int:
    """Fixed integer relevance over normalized title/body fields."""
    score = 0
    for clause in query.positive:
        if count_token_occurrences(search_title, clause.normalized):
            score += TITLE_CLAUSE_POINTS
        for term in clause.tokens:
            score += TITLE_TERM_POINTS * min(
                TITLE_TERM_CAP, count_token_occurrences(search_title, term)
            )
            score += BODY_TERM_POINTS * min(
                BODY_TERM_CAP, count_token_occurrences(search_body, term)
            )
        if clause.is_phrase:
            score += BODY_PHRASE_POINTS * min(
                BODY_PHRASE_CAP,
                count_token_occurrences(search_body, clause.normalized),
            )
    return score


def document_matches_exclusions(query: SearchQuery, *, normalized_text: str) -> bool:
    """True when the indexed document matches any exclusion (whole conversation out)."""
    return any(
        count_token_occurrences(normalized_text, clause.normalized) for clause in query.exclusions
    )


def build_snippet(query: SearchQuery, snippet_text: str) -> SearchSnippet | None:
    """Build at most one 240-code-point plain-text snippet around the first hit."""
    if not snippet_text:
        return None
    first_clause: TextClause | None = None
    first_pos = -1
    for clause in query.positive:
        pos = _find_clause_in_display(snippet_text, clause)
        if pos >= 0 and (first_pos < 0 or pos < first_pos):
            first_pos = pos
            first_clause = clause
    if first_clause is None or first_pos < 0:
        return None

    if len(snippet_text) <= SNIPPET_CODE_POINTS:
        start = 0
        end = len(snippet_text)
    else:
        start = max(0, first_pos - SNIPPET_CODE_POINTS // 3)
        prefix = start > 0
        suffix = len(snippet_text) - start > SNIPPET_CODE_POINTS - int(prefix)
        content_limit = SNIPPET_CODE_POINTS - int(prefix) - int(suffix)
        if not suffix:
            start = len(snippet_text) - content_limit
        end = min(len(snippet_text), start + content_limit)
    text = snippet_text[start:end]
    if start > 0:
        text = "…" + text.lstrip()
    if end < len(snippet_text):
        text = text.rstrip() + "…"

    matched: list[str] = []
    snippet_norm = " ".join(normalize_search_terms(text))
    seen: set[str] = set()
    for clause in query.positive:
        for term in clause.tokens:
            if term not in seen and count_token_occurrences(snippet_norm, term):
                matched.append(term)
                seen.add(term)
    return SearchSnippet(text=text, matched_terms=tuple(matched))


def escape_fts5_token(token: str) -> str:
    """Quote one FTS5 token/phrase for use as a SQL parameter fragment."""
    escaped = token.replace('"', '""')
    return f'"{escaped}"'


def _tokenize(raw: str) -> list[_RawToken]:
    tokens: list[_RawToken] = []
    i = 0
    n = len(raw)
    while i < n:
        while i < n and raw[i].isspace():
            i += 1
        if i >= n:
            break
        negative = False
        if raw[i] == "-" and i + 1 < n and not raw[i + 1].isspace():
            negative = True
            i += 1
        if i < n and raw[i] == '"':
            i += 1
            chars: list[str] = []
            closed = False
            while i < n:
                ch = raw[i]
                if ch == "\\":
                    if i + 1 >= n:
                        raise DomainError(ErrorCode.INVALID_SEARCH_QUERY, "invalid escape")
                    if raw[i + 1] not in {'"', "\\"}:
                        raise DomainError(ErrorCode.INVALID_SEARCH_QUERY, "invalid escape")
                    chars.append(raw[i + 1])
                    i += 2
                    continue
                if ch == '"':
                    closed = True
                    i += 1
                    break
                chars.append(ch)
                i += 1
            if not closed:
                raise DomainError(ErrorCode.INVALID_SEARCH_QUERY, "unclosed quote")
            phrase = "".join(chars)
            if not phrase.strip():
                raise DomainError(ErrorCode.INVALID_SEARCH_QUERY, "empty phrase")
            tokens.append(_RawToken(kind="phrase", value=phrase, negative=negative))
            continue
        start = i
        while i < n and not raw[i].isspace():
            i += 1
        word = raw[start:i]
        if not word:
            continue
        if ":" in word:
            key, _, value = word.partition(":")
            key_folded = key.casefold()
            if key_folded in {"is", "has", "harness", "before", "after"}:
                tokens.append(
                    _RawToken(
                        kind="filter",
                        value=value,
                        negative=negative,
                        filter_key=key_folded,
                    )
                )
                continue
            if key_folded and value != word:
                raise DomainError(ErrorCode.INVALID_SEARCH_QUERY, "unknown operator")
        tokens.append(_RawToken(kind="word", value=word, negative=negative))
    return tokens


def _parse_day(value: str) -> datetime:
    if not _DATE_RE.match(value):
        raise DomainError(ErrorCode.INVALID_SEARCH_QUERY, "invalid date")
    try:
        return datetime(int(value[0:4]), int(value[5:7]), int(value[8:10]), tzinfo=UTC)
    except ValueError as exc:
        raise DomainError(ErrorCode.INVALID_SEARCH_QUERY, "invalid date") from exc


def _digest(
    positive: list[TextClause],
    exclusions: list[TextClause],
    filters: SearchFilters,
) -> str:
    payload = {
        "p": [[list(c.tokens), c.is_phrase] for c in positive],
        "e": [[list(c.tokens), c.is_phrase] for c in exclusions],
        "f": {
            "pinned": filters.pinned,
            "archived": filters.archived,
            "has_interaction": filters.has_interaction,
            "harness": filters.harness.value if filters.harness else None,
            "before": filters.before.isoformat() if filters.before else None,
            "after": filters.after.isoformat() if filters.after else None,
        },
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _find_clause_in_display(snippet_text: str, clause: TextClause) -> int:
    """Locate the first display span whose normalized tokens match the clause."""
    parts: list[tuple[str, int]] = []
    i = 0
    n = len(snippet_text)
    while i < n:
        if not snippet_text[i].isalnum():
            i += 1
            continue
        start = i
        while i < n and snippet_text[i].isalnum():
            i += 1
        token = snippet_text[start:i].casefold()
        if token:
            parts.append((token, start))
    needle = clause.tokens
    if not needle:
        return -1
    for index in range(0, len(parts) - len(needle) + 1):
        window = tuple(parts[index + offset][0] for offset in range(len(needle)))
        if window == needle:
            return parts[index][1]
    return -1
