---
type: architecture
title: System Context
status: implemented
audiences:
  - developer
tags:
  - type/architecture
  - audience/developer
last_verified: 2026-09-05
verified_against_commit: 92bdf81138628204f7b58df5f1f80545abdbbde3
---

# System Context

TalkToHarnesses (tth-proxy) sits between HTTP clients and per-kind harness split services. The split architecture landed in commit `4764402`.

## Components

- Host Django (or a custom persistence host) owns settings, users, database, and the ASGI/worker process.
- `TalkToHarnessesService` is the in-process facade.
- A generic `RemoteHarnessAdapter` per conversation drives one split service (`tth-grok` … `tth-muse`) over HTTP+SSE; `SandboxManager` resolves an explicit URL or boots and reuses one Docker container per enabled kind.
- The `tth-types` package carries the shared wire schemas between proxy and splits.
- The relational database is canonical for conversations, events, and commands.
- Optional HTTP clients call `/api/v1` (unchanged by the split).

## Boundaries

Harness CLIs run in their split service, not in the proxy runtime. Every split runs in a proxy-managed container spawned on demand. The proxy adapter is provider-neutral and installs no CLIs, while sandbox lifecycle includes limited provider credential setup such as Grok auth seeding. OpenTelemetry is a no-op without a host SDK.

## Related

- [Layered architecture](layered-architecture.md)
- [Architecture and integrations](../maps/architecture-and-integrations.md)
- [Target users](../concepts/target-users.md)
