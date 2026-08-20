---
type: architecture
title: Persistence and Event Sequencing
status: implemented
audiences:
  - developer
tags:
  - type/architecture
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Persistence and Event Sequencing

The relational database is authoritative. Django ORM implements the `Persistence` protocol. Conversations, turns, messages, interactions, activities, process records, usage, and an append-only event log are stored together.

Each conversation allocates a monotonically increasing sequence in the same transaction that persists normalized state and `ConversationEvent`. Uniqueness on `(conversation_id, sequence)` rejects duplicates. Streaming deltas buffer at most 50 ms and publish only after commit. SSE replay uses that sequence.

A runtime refuses to execute a turn when no persistence implementation is configured.

## Related

- [Persistence decision](../decisions/persistence.md)
- [Event sequencing decision](../decisions/event-sequencing.md)
- [Conversation event](../domain/conversation-event.md)
- [Submit turns and stream events](../requirements/submit-turns-and-stream-events.md)
