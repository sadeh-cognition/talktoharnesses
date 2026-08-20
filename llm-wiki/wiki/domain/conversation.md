---
type: domain
title: Conversation
status: implemented
audiences:
  - product
  - developer
tags:
  - type/domain
  - capability/conversations
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Conversation

A conversation is an owner-scoped durable session. Status values include idle, running, waiting, background-active, and archived. Title display prefers native, then manual, then derived.

Pin, archive, snooze, soft-delete, and retention-exempt flags are stored on the conversation. `next_event_sequence` and `version` support optimistic concurrency and SSE replay. One active binding and at most one active turn are attached at a time.

## Related

- [Create and manage conversations](../requirements/create-and-manage-conversations.md)
- [Harness instance](harness-instance.md)
- [Turn and command](turn-and-command.md)
- [Conversation event](conversation-event.md)
