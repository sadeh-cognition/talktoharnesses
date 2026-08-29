---
type: overview
title: TalkToHarnesses Overview
aliases:
  - TTH
status: maintained
audiences:
  - product
  - developer
tags:
  - type/overview
last_verified: 2026-08-29
verified_against_commit: 47644027875773ba520cbfdd9f978d196a548802
---

# TalkToHarnesses Overview

TalkToHarnesses (TTH) is a Python 3.11+ proxy that unifies six coding-agent harness split services behind one adapter protocol, a persistence-backed asynchronous facade, and optional authenticated HTTP/SSE APIs.

The proxy exposes Grok, Cursor, Codex, Claude Code, OpenCode, and Prime Agent through a generic remote adapter. Hosts install optional extras for Django, PostgreSQL, and the official HTTP client. Provider adapters, SDK dependencies, executable discovery, and compatibility data live in the per-kind split projects.

## Main capabilities

- [Unified harness adapters](capabilities/unified-harness-adapters.md)
- [Persistent conversations and turns](capabilities/persistent-conversations.md)
- [Approvals and structured questions](capabilities/approvals-and-questions.md)
- [Django HTTP and SSE surface](capabilities/django-http-sse.md)
- [Floor-and-probe compatibility](capabilities/floor-and-probe-compatibility.md)

## Technical shape

Domain models and events are provider-neutral. `TalkToHarnessesService` is the asynchronous facade over persistence, adapters, and durable commands. Django Ninja routes are thin HTTP adapters over that facade. One split-owned supervised runtime per active conversation executes harness work. Compatibility is a split-owned floor plus live probe; models, modes, and efforts come from the split's installed CLI or SDK.

## Related

- [Product overview](maps/product-overview.md)
- [Developer overview](maps/developer-overview.md)
- [System context](architecture/system-context.md)
- [Target users](concepts/target-users.md)
