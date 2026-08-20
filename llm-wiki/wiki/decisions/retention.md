---
type: decision
title: Retention Decision
status: implemented
audiences:
  - developer
tags:
  - type/decision
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
sources:
  - raw/engineering/adr-0006-retention.md
---

# Retention Decision

An externally scheduled Django management command enforces owner-scoped calendar-month retention. Default is six months when no policy is stored.

For active conversations it deletes complete expired turn aggregates while skipping running or background-active conversations and retention-exempt conversations. Soft-deleted conversations are permanently deleted after the owner's period from `deleted_at`. Workspace files are never deleted.

## Related

- [ADR 0006 source](../../raw/engineering/adr-0006-retention.md)
- [Retain and prune transcripts](../requirements/retain-and-prune-transcripts.md)
- [Search, retention, and transcripts](../capabilities/search-retention-transcripts.md)
