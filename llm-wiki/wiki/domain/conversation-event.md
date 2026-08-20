---
type: domain
title: Conversation Event
status: implemented
audiences:
  - developer
tags:
  - type/domain
  - capability/conversations
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Conversation Event

A conversation event is a typed payload with a conversation-local monotonic sequence. Payloads include session lifecycle, turn lifecycle, assistant deltas and completion, tools, plans, activities, interactions, usage/cost, process signals, and metadata changes.

SSE and the official client replay by sequence. The envelope is provider-neutral; adapters normalize native streams before persistence.

## Related

- [Persistence and event sequencing](../architecture/persistence-and-event-sequencing.md)
- [Submit turns and stream events](../requirements/submit-turns-and-stream-events.md)
- [Conversation](conversation.md)
