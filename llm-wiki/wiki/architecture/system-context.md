---
type: architecture
title: System Context
status: implemented
audiences:
  - developer
tags:
  - type/architecture
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# System Context

TalkToHarnesses is a library-plus-optional-Django-app that sits between a host process and local coding-agent CLIs.

## Components

- Host Django (or a custom persistence host) owns settings, users, database, and the ASGI/worker process.
- `TalkToHarnessesService` is the in-process facade.
- Provider adapters talk to Grok, Cursor, Codex, Claude Code, OpenCode, or Prime Agent.
- The relational database is canonical for conversations, events, and commands.
- Optional HTTP clients, including Agentbahn, call `/api/v1`.

## Boundaries

Harness processes run as the host OS user. The package does not sandbox, install CLIs, or manage host middleware. OpenTelemetry is a no-op without a host SDK.

## Related

- [Layered architecture](layered-architecture.md)
- [Architecture and integrations](../maps/architecture-and-integrations.md)
- [Target users](../concepts/target-users.md)
