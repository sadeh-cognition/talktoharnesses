---
type: glossary
title: Glossary
status: maintained
audiences:
  - product
  - developer
tags:
  - type/glossary
  - audience/product
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Glossary

## Adapter

A provider-specific implementation of `HarnessAdapter`. Methods are fixed for all harnesses. See [Adapter protocol](interfaces/adapter-protocol.md).

## Binding

A `ConversationHarnessBinding` that records the harness kind, configuration, native session id, and launch snapshot for one conversation attachment. Switching harnesses closes the active binding and opens another.

## Conversation

Owner-scoped durable session with status, titles, pinning, archive, snooze, retention, and an event sequence. See [Conversation](domain/conversation.md).

## Floor

The minimum packaged identity and platforms an adapter will drive. Identities older than the floor or on unpublished platforms fail probe. See [Floor-and-probe compatibility](capabilities/floor-and-probe-compatibility.md).

## Harness

A named owner-owned configuration of kind, working directory, optional executable path, model, mode, effort, and yolo. See [Harness instance](domain/harness-instance.md).

## Interaction

A pending approval or structured question that pauses a turn until resolved. See [Interaction](domain/interaction.md).

## Owner

The Django user identifier used for every domain query. Globally unique ids never bypass owner filtering.

## Probe

Live inspection of an installed CLI against the packaged floor, returning capabilities, models, modes, efforts, and a version advisory.

## SSE

Server-Sent Events stream at `GET /conversations/{id}/events`. Replay uses conversation-local sequence ids and `Last-Event-ID`.

## Turn

One user prompt execution with status, messages, tools, and optional queued follow-ups. See [Turn and command](domain/turn-and-command.md).

## Yolo

Harness-creation flag that suppresses approval prompts through provider-native mechanisms. Structured questions remain interactive.

## Related

- [TalkToHarnesses overview](overview.md)
- [Wiki index](index.md)
