---
type: requirement
title: Submit Turns and Stream Events
status: implemented
audiences:
  - product
  - developer
tags:
  - type/requirement
  - capability/conversations
  - capability/http
  - status/implemented
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
sources:
  - raw/engineering/adr-0002-event-sequencing.md
---

# Submit Turns and Stream Events

## Intent

An owner can submit an idempotent turn and observe committed events in conversation-local order, including after reconnect.

## Current behavior

`POST /conversations/{id}/turns` accepts a prompt, optional model override, and `Idempotency-Key`. The command is persisted and executed by the conversation's runtime. `GET /conversations/{id}/events` is an SSE stream. `Last-Event-ID` replays from the conversation sequence. Deltas buffer at most 50 ms and publish only after commit.

## Gap

No gap remains against the event-sequencing contract.

## Acceptance criteria

- Submit is idempotent for a repeated key on the same conversation.
- One active turn at a time; additional prompts may queue.
- SSE events use the conversation sequence as id.
- Reconnect with `Last-Event-ID` does not reorder committed events.
- Uncommitted delta batches may be lost on crash.

## Implementation evidence

- `src/talktoharnesses/application/service.py` (`submit_turn`, `replay_events`)
- `src/talktoharnesses/application/delta_batcher.py`
- `src/talktoharnesses/django/api/sse.py`
- `src/talktoharnesses/django/api/routes.py`

## Test evidence

- `tests/property/test_event_ordering.py`
- `tests/property/test_one_active_turn.py`
- `tests/e2e/test_phase5_api_gate.py`
- `tests/e2e/test_phase10_definition_of_done.py`
- `tests/unit/django/test_sse.py`

## Related

- [Persistent conversations and turns](../capabilities/persistent-conversations.md)
- [Conversation event](../domain/conversation-event.md)
- [Event sequencing decision](../decisions/event-sequencing.md)
- [HTTP and SSE API](../interfaces/http-and-sse-api.md)
