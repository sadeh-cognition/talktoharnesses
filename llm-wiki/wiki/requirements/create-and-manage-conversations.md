---
type: requirement
title: Create and Manage Conversations
status: implemented
audiences:
  - product
  - developer
tags:
  - type/requirement
  - capability/conversations
  - status/implemented
last_verified: 2026-09-09
verified_against_commit: aba8979
sources:
  - raw/engineering/adr-0001-persistence.md
---

# Create and Manage Conversations

## Intent

An owner can create a conversation bound to a harness, list conversations, pin, archive, snooze, and soft-delete them. Globally unique ids never bypass owner filtering.

## Current behavior

`POST /conversations` creates a conversation and active binding. List supports cursor pagination and optional archived inclusion. Archive, pin, snooze, unarchive, unpin, unsnooze, and soft-delete are owner-scoped mutations that emit conversation metadata events. Display title prefers native, then manual, then derived, then a default.

`ConversationDetail` publishes the active binding's `harness_kind`, `harness_id`, `model`, `mode`, `effort`, and `yolo`. A conversation outlives the harness it was opened on, so a client resuming one reads its harness identity and approval policy here rather than from a harness record that may be gone. All six are null when the conversation has no binding, and `harness_id` is additionally null on legacy bindings that never recorded one.

## Gap

No gap remains against the persistence and ownership contract.

## Acceptance criteria

- Create binds the conversation to an owned harness and returns a snapshot.
- List, get, and mutations fail for another owner's id.
- Pin, archive, and snooze persist and reverse through matching un-* endpoints.
- Soft-delete hides the conversation from ordinary list without removing workspace files.
- Get reports the binding's harness id and approval policy after that harness is deleted.

## Implementation evidence

- `src/talktoharnesses/application/service.py` (conversation lifecycle methods)
- `src/talktoharnesses/domain/models.py` (`Conversation`, `ConversationHarnessBinding`, `ConversationDetail`)
- `src/talktoharnesses/django/persistence.py` (`_build_conversation_snapshot`)
- `src/talktoharnesses/django/api/routes.py`

## Test evidence

- `tests/e2e/test_phase5_api_gate.py`
- `tests/contract/test_facade_persistence.py`
- `tests/unit/django/test_api.py`
- `tests/unit/application/test_service.py::test_conversation_detail_reports_the_binding_harness_and_policy`

## Related

- [Persistent conversations and turns](../capabilities/persistent-conversations.md)
- [Conversation](../domain/conversation.md)
- [Persistence decision](../decisions/persistence.md)
- [Host Django and run a conversation](../journeys/host-django-and-run-a-conversation.md)
