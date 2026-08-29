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

The **library/wheel** stays API-only: with no SDK configured, its instrumentation is a no-op, and there is no package-owned `otel` extra. Secret-bearing fields are excluded from attributes.

The **host process** (`host/telemetry.py`) and every **split service** (`tth-<kind>/src/<pkg>/telemetry.py`) export traces, metrics, and logs by default via OTLP/HTTP. Set `OTEL_EXPORTER_OTLP_ENDPOINT=false` (or `0`, case-insensitive) to disable all signals; any other value is the collector endpoint, and unset uses the SDK default (`http://localhost:4318`). Because export is on by default, the SDK/exporter packages are required at startup (proxy dev group; split `[project].dependencies`) — missing packages raise a `RuntimeError` unless opted out. The host bridges both loguru and stdlib logging into the log exporter; splits bridge stdlib logging via a root-logger handler (uvicorn's own loggers do not propagate and are not exported). The proxy's `SandboxManager` injects the endpoint into split containers, rewriting unset/localhost values to `http://host.docker.internal:4318` with a `host-gateway` extra-hosts mapping.

## Related

- [System context](system-context.md)
- [Technology stack](technology-stack.md)
- [Layered architecture](layered-architecture.md)
