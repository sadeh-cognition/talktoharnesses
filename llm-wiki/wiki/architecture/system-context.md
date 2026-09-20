---
type: architecture
title: System Context
status: implemented
audiences:
  - developer
tags:
  - type/architecture
  - audience/developer
last_verified: 2026-09-20
verified_against_commit: bb531a658b1e9ddbe510b3fe07ab4e7170b03fdb
---

# System Context

TalkToHarnesses (tth-proxy) sits between HTTP clients and per-kind harness split services. The split architecture landed in commit `4764402`.

## Components

- Host Django (or a custom persistence host) owns settings, users, database, and the ASGI/worker process.
- `TalkToHarnessesService` is the in-process facade.
- A generic `RemoteHarnessAdapter` per conversation drives one split service (`tth-grok` … `tth-muse`) over HTTP+SSE; `ScopedSandboxManager` resolves an immutable project policy and boots or reuses a Docker scope for its provider and mount set.
- The `tth-types` package carries the shared wire schemas between proxy and splits.
- The relational database is canonical for conversations, events, and commands.
- Optional HTTP clients call `/api/v1` (unchanged by the split).

## Boundaries

Harness CLIs run in their split service, not in the proxy runtime. Every split runs in a proxy-managed container spawned on demand. The proxy adapter is provider-neutral and installs no CLIs, while each scope has a separate credential gateway, internal network, and home/data volumes. The gateway substitutes scoped handles for host credentials and enforces default-deny HTTPS egress. OpenTelemetry is a no-op without a host SDK.

The recorded commit is the inspected baseline; these policy boundaries are
implemented in the associated uncommitted worktree changes.

## Related

- [Layered architecture](layered-architecture.md)
- [Architecture and integrations](../maps/architecture-and-integrations.md)
- [Target users](../concepts/target-users.md)

- [Project sandbox policies](../requirements/project-sandbox-policies.md)
