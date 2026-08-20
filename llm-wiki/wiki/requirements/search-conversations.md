---
type: requirement
title: Search Conversations
status: implemented
audiences:
  - product
  - developer
tags:
  - type/requirement
  - capability/search
  - status/implemented
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
sources:
  - raw/engineering/search-retention-transcripts.md
---

# Search Conversations

## Intent

An owner can search conversations with a documented query grammar and receive ranked hits with optional snippets. Search does not expose native sessions or raw SQL.

## Current behavior

`GET /conversations/search` and `search_conversations` accept `q`, `cursor`, and `limit`. The grammar supports unquoted terms, quoted phrases, exclusions, `is:pinned`, `is:archived`, `has:interaction`, `harness:` kinds, and `before:`/`after:` dates. At least one positive term is required. Unknown operators and invalid queries raise `invalid_search_query`. Order is rank, then `updated_at`, then UUID.

## Gap

No gap remains against the documented search grammar. OR, parentheses, wildcards, field selectors, fuzzy matching, stemming, autocomplete, and saved searches are out of scope.

## Acceptance criteria

- Successful results are `Page[ConversationSearchHit]`.
- Invalid grammar returns HTTP 400 with `invalid_search_query`.
- Cursors are opaque and rejected when reused with a different normalized query.
- Hits are owner-scoped.

## Implementation evidence

- `src/talktoharnesses/application/search_query.py`
- `src/talktoharnesses/application/search_documents.py`
- `src/talktoharnesses/application/service.py` (`search_conversations`)

## Test evidence

- `tests/test_phase8_fts.py`
- `tests/unit/application/test_search_query.py`
- `tests/unit/application/test_search_documents.py`
- `tests/test_docs_ops.py`

## Related

- [Search, retention, and transcripts](../capabilities/search-retention-transcripts.md)
- [Search conversations and apply retention](../journeys/search-conversations-and-apply-retention.md)
- [Engineering search source](../../raw/engineering/search-retention-transcripts.md)
