---
type: map
title: Architecture and Integrations
status: maintained
audiences:
  - developer
tags:
  - type/map
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Architecture and Integrations

TalkToHarnesses sits between host applications and local coding-agent CLIs.

## Internal layers

[Layered architecture](../architecture/layered-architecture.md) keeps domain models free of Django and provider SDKs. The application facade coordinates persistence, commands, and adapters. Django is an optional HTTP surface.

## External boundaries

- Host Django settings, ASGI process, database, and JWT users.
- Six provider CLIs or SDKs, each behind [adapter protocol](../interfaces/adapter-protocol.md).
- Optional OpenTelemetry SDK/exporter installed by the host.
- Official HTTP client for remote consumers such as Agentbahn.

## Persistence and workers

SQLite is a single-supervisor profile. PostgreSQL supports multi-worker claims and leases. No external message broker is added.

## Related

- [System context](../architecture/system-context.md)
- [Developer overview](developer-overview.md)
- [HTTP API map](http-api.md)
- [Compatibility and adapters](compatibility-and-adapters.md)
