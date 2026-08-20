---
type: map
title: Product Overview
status: maintained
audiences:
  - product
tags:
  - type/map
  - audience/product
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Product Overview

TalkToHarnesses gives [library embedders, Django hosts, and remote HTTP clients](../concepts/target-users.md) one persistence-backed way to drive local coding-agent CLIs.

## Capabilities

- [Unified harness adapters](../capabilities/unified-harness-adapters.md) cover Grok, Cursor, Codex, Claude Code, OpenCode, and Prime Agent.
- [Persistent conversations and turns](../capabilities/persistent-conversations.md) keep canonical transcripts independent of native harness state.
- [Approvals and structured questions](../capabilities/approvals-and-questions.md) pause turns for human or rule-based answers.
- [Django HTTP and SSE surface](../capabilities/django-http-sse.md) exposes authenticated APIs and replayable streams.
- [Floor-and-probe compatibility](../capabilities/floor-and-probe-compatibility.md) accepts identities at or above the packaged floor.
- [Search, retention, and transcripts](../capabilities/search-retention-transcripts.md) cover ranked search, owner-scoped cleanup, and portable documents.
- [Isolated harness runtimes](../capabilities/isolated-harness-runtimes.md) keep one supervised process per active conversation.
- [Official HTTP client](../capabilities/official-http-client.md) is the supported remote consumer.

## Journeys

- [Host Django and run a conversation](../journeys/host-django-and-run-a-conversation.md)
- [Resolve a pending approval](../journeys/resolve-a-pending-approval.md)
- [Search conversations and apply retention](../journeys/search-conversations-and-apply-retention.md)

## Requirements

- [Probe and configure harnesses](../requirements/probe-and-configure-harnesses.md)
- [Create and manage conversations](../requirements/create-and-manage-conversations.md)
- [Submit turns and stream events](../requirements/submit-turns-and-stream-events.md)
- [Resolve approvals and structured questions](../requirements/resolve-approvals-and-structured-questions.md)
- [Steer, interrupt, and switch harness](../requirements/steer-interrupt-and-switch-harness.md)
- [Queue and edit prompts](../requirements/queue-and-edit-prompts.md)
- [Apply approval rules](../requirements/apply-approval-rules.md)
- [Search conversations](../requirements/search-conversations.md)
- [Retain and prune transcripts](../requirements/retain-and-prune-transcripts.md)
- [Export and import transcripts](../requirements/export-and-import-transcripts.md)
- [Authenticate with JWT](../requirements/authenticate-with-jwt.md)
- [Host Django ASGI with readiness](../requirements/host-django-asgi-with-readiness.md)

## Related

- [Requirements by status](requirements-by-status.md)
- [Developer overview](developer-overview.md)
- [TalkToHarnesses overview](../overview.md)
