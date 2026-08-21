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
last_verified: 2026-08-21
verified_against_commit: 3f90f85a37028a1ba0498cff641ef5c8a1bec6d7
---

# TalkToHarnesses Overview

TalkToHarnesses (TTH) is a Python 3.11+ package that unifies six coding-agent harnesses behind one adapter protocol, a persistence-backed asynchronous facade, and optional authenticated HTTP/SSE APIs.

One distribution exposes Grok, Cursor, Codex, Claude Code, OpenCode, and Prime Agent adapters. Hosts install optional extras for Django, PostgreSQL, the official HTTP client, and provider SDKs. External CLIs are located on PATH or a TalkToHarnesses process environment override; the package never installs or upgrades them.

## Main capabilities

- [Unified harness adapters](capabilities/unified-harness-adapters.md)
- [Persistent conversations and turns](capabilities/persistent-conversations.md)
- [Approvals and structured questions](capabilities/approvals-and-questions.md)
- [Django HTTP and SSE surface](capabilities/django-http-sse.md)
- [Floor-and-probe compatibility](capabilities/floor-and-probe-compatibility.md)

## Technical shape

Domain models and events are provider-neutral. `TalkToHarnessesService` is the asynchronous facade over persistence, adapters, and durable commands. Django Ninja routes are thin HTTP adapters over that facade. One supervised runtime per active conversation executes harness work as the Django OS user. Compatibility is a packaged floor plus live probe; models, modes, and efforts come from the installed CLI.

## Related

- [Product overview](maps/product-overview.md)
- [Developer overview](maps/developer-overview.md)
- [System context](architecture/system-context.md)
- [Target users](concepts/target-users.md)
