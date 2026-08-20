---
type: journey
title: Search Conversations and Apply Retention
status: implemented
audiences:
  - product
  - developer
tags:
  - type/journey
  - capability/search
  - capability/retention
  - status/implemented
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Search Conversations and Apply Retention

This journey traces ranked search, policy preview, and scheduled cleanup.

## 1. Search

The owner queries `GET /conversations/search?q=...` with the documented grammar and pages ranked hits. Requirement: [Search conversations](../requirements/search-conversations.md).

## 2. Review retention policy

The owner reads or replaces the calendar-month policy and previews eligible work. Requirement: [Retain and prune transcripts](../requirements/retain-and-prune-transcripts.md).

## 3. Run cleanup

An external scheduler runs `talktoharnesses_cleanup`. Expired turn aggregates are deleted. Native sessions may rotate. Workspace files are left on disk.

## Related

- [Search, retention, and transcripts](../capabilities/search-retention-transcripts.md)
- [Export and import transcripts](../requirements/export-and-import-transcripts.md)
- [Retention decision](../decisions/retention.md)
