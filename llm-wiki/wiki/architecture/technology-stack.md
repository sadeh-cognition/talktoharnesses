---
type: architecture
title: Technology Stack
status: implemented
audiences:
  - developer
tags:
  - type/architecture
  - audience/developer
last_verified: 2026-09-05
verified_against_commit: 92bdf81138628204f7b58df5f1f80545abdbbde3
---

# Technology Stack

Python 3.11+ with Pydantic v2 domain models. Optional Django 5.2, Django Ninja, PyJWT, and Uvicorn. PostgreSQL via Psycopg 3; SQLite with FTS5. httpx is a core dependency (remote split adapters and the official client); the django extra adds docker-py for sandbox lifecycle. OpenTelemetry API is a core dependency. The shared `tth-types` package (pydantic-only) is a path dependency during development.

Per-kind SDK pins live in the top-level split projects: `tth-codex` pins `openai-codex`, `tth-claude` pins `claude-agent-sdk`; Grok, Cursor, OpenCode, Prime Agent, and Muse Code splits install their external CLIs in their Docker images.

Packaging uses uv. Versions are CalVer (`YYYY.M.PATCH`). Tests use pytest, pytest-django, pytest-asyncio, Hypothesis, and coverage gates.

## Related

- [System context](system-context.md)
- [Layered architecture](layered-architecture.md)
- [Development guidelines](../operations/development-guidelines.md)
