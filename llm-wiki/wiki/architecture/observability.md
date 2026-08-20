---
type: architecture
title: Observability
status: implemented
audiences:
  - developer
tags:
  - type/architecture
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Observability

The package instruments with the OpenTelemetry API only. Span and metric names, attributes, and recording helpers are fixed. Callers pass enums or allowlisted strings — never arbitrary attribute dictionaries or exception objects as payload.

With no host SDK configured, instrumentation is a no-op. There is no package-owned `otel` extra. Secret-bearing fields are excluded from attributes.

## Related

- [System context](system-context.md)
- [Technology stack](technology-stack.md)
- [Layered architecture](layered-architecture.md)
