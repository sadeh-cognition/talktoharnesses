---
type: capability
title: Search, Retention, and Transcripts
status: implemented
audiences:
  - product
  - developer
tags:
  - type/capability
  - capability/search
  - capability/retention
  - status/implemented
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Search, Retention, and Transcripts

Owner-scoped ranked search, calendar-month retention, and canonical transcript export/import operate on package-owned database state. They do not expose or delete provider-native sessions or workspace files.

## Product value

Hosts search conversations with a documented query grammar, preview and run retention cleanup, exempt conversations, and move transcripts between harnesses.

## Current implementation

Search uses FTS5 on SQLite and PostgreSQL full-text search. Retention is an externally scheduled Django management command. Transcripts are canonical documents independent of native session ids.

## Requirements

- [Search conversations](../requirements/search-conversations.md)
- [Retain and prune transcripts](../requirements/retain-and-prune-transcripts.md)
- [Export and import transcripts](../requirements/export-and-import-transcripts.md)

## Related

- [Retention decision](../decisions/retention.md)
- [Search conversations and apply retention](../journeys/search-conversations-and-apply-retention.md)
- [Engineering search, retention, and transcripts source](../../raw/engineering/search-retention-transcripts.md)
