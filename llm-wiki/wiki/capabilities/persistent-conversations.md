---
type: capability
title: Persistent Conversations and Turns
status: implemented
audiences:
  - product
  - developer
tags:
  - type/capability
  - capability/conversations
  - status/implemented
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Persistent Conversations and Turns

Canonical conversations, turns, messages, and events live in the relational database. Native harness state is a resumable execution detail, not durable truth.

## Product value

Clients can reconnect, page history, export transcripts, and resume work after process restart. Pin, archive, snooze, and soft-delete are owner-scoped conversation controls.

## Current implementation

`TalkToHarnessesService` creates conversations bound to a harness instance, submits turns as durable commands, and publishes committed events in conversation-local sequence. One active turn runs at a time; additional prompts may queue. Persistence is mandatory for execution.

## Requirements

- [Create and manage conversations](../requirements/create-and-manage-conversations.md)
- [Submit turns and stream events](../requirements/submit-turns-and-stream-events.md)
- [Queue and edit prompts](../requirements/queue-and-edit-prompts.md)
- [Export and import transcripts](../requirements/export-and-import-transcripts.md)

## Related

- [Conversation](../domain/conversation.md)
- [Turn and command](../domain/turn-and-command.md)
- [Persistence decision](../decisions/persistence.md)
- [Host Django and run a conversation](../journeys/host-django-and-run-a-conversation.md)
