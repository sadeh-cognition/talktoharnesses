# ADR 0002: Event Sequencing

- **Status:** Accepted
- **Date:** 2026-08-07

## Context

Streaming clients reconnect with a conversation cursor and must observe
committed events in their durable order. Database-generated global IDs do not
provide the required conversation-local sequence.

## Decision

Allocate a monotonically increasing sequence within each conversation in the
same transaction that persists the normalized state and `ConversationEvent`.
Enforce uniqueness on `(conversation_id, sequence)` to reject duplicates.
Buffer streaming deltas for at most 50 ms and publish their SSE events only
after the transaction commits.

## Consequences

SSE replay and `Last-Event-ID` use the conversation sequence. A crash may lose
only the current uncommitted delta batch; committed state must never be
published out of order.
