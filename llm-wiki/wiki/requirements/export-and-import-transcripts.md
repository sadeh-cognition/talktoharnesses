---
type: requirement
title: Export and Import Transcripts
status: implemented
audiences:
  - product
  - developer
tags:
  - type/requirement
  - capability/conversations
  - status/implemented
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
sources:
  - raw/engineering/search-retention-transcripts.md
---

# Export and Import Transcripts

## Intent

An owner can export a canonical transcript document and import one onto a harness to create a new conversation. Native session ids and workspace files are not part of the document.

## Current behavior

`GET /conversations/{id}/transcript` returns `TranscriptDocument`. `POST /conversations/import` creates a conversation from a document and harness id, emitting a transcript-imported event. Import does not resume a provider-native session.

## Gap

No gap remains against the documented portability contract.

## Acceptance criteria

- Export includes canonical messages and tools for the conversation.
- Import creates a new owned conversation bound to the given harness.
- Native session identifiers are not required to import.
- Import fails closed on invalid documents.

## Implementation evidence

- `src/talktoharnesses/application/service.py` (`export_transcript`, `import_transcript`)
- `src/talktoharnesses/domain/transcripts.py`
- `src/talktoharnesses/application/transcripts.py`

## Test evidence

- `tests/unit/application/test_transcript_import.py`
- `tests/unit/domain/test_transcripts.py`
- `tests/unit/django/test_api.py`

## Related

- [Search, retention, and transcripts](../capabilities/search-retention-transcripts.md)
- [Create and manage conversations](create-and-manage-conversations.md)
- [Persistent conversations and turns](../capabilities/persistent-conversations.md)
