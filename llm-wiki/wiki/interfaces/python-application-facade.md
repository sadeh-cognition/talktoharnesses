---
type: interface
title: Python Application Facade
status: implemented
audiences:
  - developer
tags:
  - type/interface
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Python Application Facade

`TalkToHarnessesService` is the asynchronous facade over persistence, adapters, and durable commands. Django routes and custom hosts should use this type rather than provider adapters or ORM models.

Public application exports are `TalkToHarnessesService`, `Persistence`, `CommittedEventBroker`, `CommittedEventPublisher`, `ConversationWakeup`, and `StreamingTextRedactor`.

The service requires a persistence implementation to execute turns. It owns command processing, worker coordination, interaction brokering, search, retention preview, transcript import/export, and committed event publish.

## Related

- [Layered architecture](../architecture/layered-architecture.md)
- [HTTP and SSE API](http-and-sse-api.md)
- [Persistent conversations and turns](../capabilities/persistent-conversations.md)
