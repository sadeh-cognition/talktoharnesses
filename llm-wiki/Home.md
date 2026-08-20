---
type: map
title: TalkToHarnesses Knowledge Base
aliases:
  - TalkToHarnesses Wiki Home
status: maintained
audiences:
  - product
  - developer
tags:
  - type/map
  - audience/product
  - audience/developer
last_verified: 2026-08-20
---

# TalkToHarnesses Knowledge Base

TalkToHarnesses is a unified coding-agent harness interface for [library embedders, Django hosts, and Agentbahn-class products](wiki/concepts/target-users.md).

This vault connects public contracts to architecture, implementation, and tests. Begin with the view that matches your question:

- [Product overview](wiki/maps/product-overview.md) explains capabilities, journeys, requirements, and status.
- [Developer overview](wiki/maps/developer-overview.md) explains architecture, interfaces, decisions, operations, and evidence.
- [Requirements by status](wiki/maps/requirements-by-status.md) shows the current delivery picture.
- [Wiki index](wiki/index.md) catalogs every maintained area.

## Core capabilities

- [Unified harness adapters](wiki/capabilities/unified-harness-adapters.md)
- [Persistent conversations and turns](wiki/capabilities/persistent-conversations.md)
- [Approvals and structured questions](wiki/capabilities/approvals-and-questions.md)
- [Django HTTP and SSE surface](wiki/capabilities/django-http-sse.md)
- [Floor-and-probe compatibility](wiki/capabilities/floor-and-probe-compatibility.md)
- [Search, retention, and transcripts](wiki/capabilities/search-retention-transcripts.md)
- [Isolated harness runtimes](wiki/capabilities/isolated-harness-runtimes.md)
- [Official HTTP client](wiki/capabilities/official-http-client.md)

## How to read this vault

Approved sources under `raw/` describe intended behavior. Code and tests at a recorded commit describe current behavior. When those disagree, requirement pages preserve the difference as an explicit gap rather than choosing one silently.

Human operator procedures remain in the repository `docs/` directory. This vault snapshots those sources and synthesizes them; it does not replace them.

## Related

- [TalkToHarnesses overview](wiki/overview.md)
- [Glossary](wiki/glossary.md)
- [Wiki maintenance](wiki/operations/wiki-maintenance.md)
