---
type: domain
title: Conversation Event
status: implemented
audiences:
  - developer
tags:
  - type/domain
  - capability/conversations
last_verified: 2026-08-21
verified_against_commit: c996cbcd23b7cbf4f6b4d70422ab17ce715661bf
---

# Conversation Event

A conversation event is a typed payload with a conversation-local monotonic sequence. Payloads include session lifecycle, turn lifecycle, assistant deltas and completion, tools, plans, activities, interactions, usage/cost, process signals, and metadata changes.

SSE and the official client replay by sequence. The envelope is provider-neutral; adapters normalize native streams before persistence.

`usage_updated` attributes optional input, output, total, and cached-input token
counts to a turn. Providers may omit categories they do not report; adapters do
not derive them from other fields. A turn may report several times while it
runs, and every report carries the turn's totals so far rather than the
increment since the last one, so a later report supersedes an earlier one for
that turn instead of adding to it. Adapters whose provider reports per-request
usage accumulate it through the shared `TurnUsage` accumulator to preserve this
meaning, and a report that trails a turn's authoritative terminal figures is
dropped rather than allowed to report a smaller total.

## Related

- [Persistence and event sequencing](../architecture/persistence-and-event-sequencing.md)
- [Submit turns and stream events](../requirements/submit-turns-and-stream-events.md)
- [Conversation](conversation.md)
- [Report harness token usage](../requirements/report-harness-token-usage.md)
