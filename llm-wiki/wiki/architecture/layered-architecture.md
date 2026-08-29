---
type: architecture
title: Layered Architecture
status: implemented
audiences:
  - developer
tags:
  - type/architecture
  - audience/developer
last_verified: 2026-08-29
verified_against_commit: 47644027875773ba520cbfdd9f978d196a548802
---

# Layered Architecture

The package is layered so Django and provider SDKs do not leak inward.

- `tth_types` (top-level project) — shared wire schemas: base config, enums, errors, events, harness models, adapter protocol, process events, split API.
- `talktoharnesses.domain` — proxy entities, projections, transitions (wire types re-exported from `tth_types`).
- `talktoharnesses.application` — `TalkToHarnessesService`, persistence protocol, commands, broker, retention, search.
- `talktoharnesses.providers` — adapter protocol re-export and registry (per-kind adapters live in top-level split projects).
- `talktoharnesses.remote` — `RemoteHarnessAdapter`, `RemoteProcessHandle`, `SandboxManager`, remote registry.
- `talktoharnesses.runtime` — runtime manager over remote sessions.
- `talktoharnesses.django` — ORM, Ninja routes, JWT, ASGI lifespan, cleanup command.
- `talktoharnesses.client` — optional async HTTP client.

Core imports must not load Django. The public `__all__` surface is contract-tested.

## Related

- [Python application facade](../interfaces/python-application-facade.md)
- [Adapter protocol](../interfaces/adapter-protocol.md)
- [System context](system-context.md)
- [Developer overview](../maps/developer-overview.md)
