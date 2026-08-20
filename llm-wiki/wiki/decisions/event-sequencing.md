---
type: decision
title: Event Sequencing Decision
status: implemented
audiences:
  - developer
tags:
  - type/decision
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
sources:
  - raw/engineering/adr-0002-event-sequencing.md
---

# Event Sequencing Decision

Streaming clients reconnect with a conversation cursor and must observe committed events in durable order. Database-generated global IDs do not provide that.

Allocate a monotonically increasing sequence within each conversation in the same transaction that persists normalized state and `ConversationEvent`. Enforce uniqueness on `(conversation_id, sequence)`. Buffer streaming deltas for at most 50 ms and publish SSE only after commit.

## Related

- [ADR 0002 source](../../raw/engineering/adr-0002-event-sequencing.md)
- [Conversation event](../domain/conversation-event.md)
- [Submit turns and stream events](../requirements/submit-turns-and-stream-events.md)
