---
type: architecture
title: Layered Architecture
status: implemented
audiences:
  - developer
tags:
  - type/architecture
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Layered Architecture

The package is layered so Django and provider SDKs do not leak inward.

- `talktoharnesses.domain` — frozen models, events, enums, errors, transitions.
- `talktoharnesses.application` — `TalkToHarnessesService`, persistence protocol, commands, broker, retention, search.
- `talktoharnesses.providers` — `HarnessAdapter` protocol, default registry, per-kind adapters, compatibility floors.
- `talktoharnesses.runtime` — process supervisor and runtime manager.
- `talktoharnesses.django` — ORM, Ninja routes, JWT, ASGI lifespan, cleanup command.
- `talktoharnesses.client` — optional async HTTP client.

Core imports must not load Django. The public `__all__` surface is contract-tested.

## Related

- [Python application facade](../interfaces/python-application-facade.md)
- [Adapter protocol](../interfaces/adapter-protocol.md)
- [System context](system-context.md)
- [Developer overview](../maps/developer-overview.md)
