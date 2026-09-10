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
last_verified: 2026-09-09
verified_against_commit: aba8979
---

# Conversation

A conversation is an owner-scoped durable session. Status values include idle, running, waiting, background-active, and archived. Title display prefers native, then manual, then derived.

Pin, archive, snooze, soft-delete, and retention-exempt flags are stored on the conversation. `next_event_sequence` and `version` support optimistic concurrency and SSE replay. One active binding and at most one active turn are attached at a time. The conversation snapshot publishes that binding's harness kind and id, model, mode, effort, and approval policy, because the binding outlives the harness record and is what a resumed turn is actually launched from.

## Related

- [Create and manage conversations](../requirements/create-and-manage-conversations.md)
- [Harness instance](harness-instance.md)
- [Turn and command](turn-and-command.md)
- [Conversation event](conversation-event.md)
