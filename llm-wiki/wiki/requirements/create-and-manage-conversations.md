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
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
sources:
  - raw/engineering/adr-0001-persistence.md
---

# Create and Manage Conversations

## Intent

An owner can create a conversation bound to a harness, list conversations, pin, archive, snooze, and soft-delete them. Globally unique ids never bypass owner filtering.

## Current behavior

`POST /conversations` creates a conversation and active binding. List supports cursor pagination and optional archived inclusion. Archive, pin, snooze, unarchive, unpin, unsnooze, and soft-delete are owner-scoped mutations that emit conversation metadata events. Display title prefers native, then manual, then derived, then a default.

## Gap

No gap remains against the persistence and ownership contract.

## Acceptance criteria

- Create binds the conversation to an owned harness and returns a snapshot.
- List, get, and mutations fail for another owner's id.
- Pin, archive, and snooze persist and reverse through matching un-* endpoints.
- Soft-delete hides the conversation from ordinary list without removing workspace files.

## Implementation evidence

- `src/talktoharnesses/application/service.py` (conversation lifecycle methods)
- `src/talktoharnesses/domain/models.py` (`Conversation`, `ConversationHarnessBinding`)
- `src/talktoharnesses/django/api/routes.py`

## Test evidence

- `tests/e2e/test_phase5_api_gate.py`
- `tests/contract/test_facade_persistence.py`
- `tests/unit/django/test_api.py`

## Related

- [Persistent conversations and turns](../capabilities/persistent-conversations.md)
- [Conversation](../domain/conversation.md)
- [Persistence decision](../decisions/persistence.md)
- [Host Django and run a conversation](../journeys/host-django-and-run-a-conversation.md)
