---
type: map
title: Architecture and Integrations
status: maintained
audiences:
  - developer
tags:
  - type/map
  - audience/developer
last_verified: 2026-08-29
verified_against_commit: 47644027875773ba520cbfdd9f978d196a548802
---

# Architecture and Integrations

TalkToHarnesses sits between host applications and per-kind coding-agent split services.

## Internal layers

[Layered architecture](../architecture/layered-architecture.md) keeps domain models free of Django and provider SDKs. The application facade coordinates persistence, commands, and generic remote adapters. Django is an optional HTTP surface.

## External boundaries

- Host Django settings, ASGI process, database, and JWT users.
- Six split services, each owning its provider CLI or SDK behind [adapter protocol](../interfaces/adapter-protocol.md).
- Optional OpenTelemetry SDK/exporter installed by the host.
- Official HTTP client for remote consumers.

## Persistence and workers

SQLite is a single-supervisor profile. PostgreSQL supports multi-worker claims and leases. No external message broker is added.

## Related

- [System context](../architecture/system-context.md)
- [Developer overview](developer-overview.md)
- [HTTP API map](http-api.md)
- [Compatibility and adapters](compatibility-and-adapters.md)
