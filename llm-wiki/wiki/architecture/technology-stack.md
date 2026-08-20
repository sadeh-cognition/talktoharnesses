---
type: architecture
title: Technology Stack
status: implemented
audiences:
  - developer
tags:
  - type/architecture
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Technology Stack

Python 3.11+ with Pydantic v2 domain models. Optional Django 5.2, Django Ninja, PyJWT, and Uvicorn. PostgreSQL via Psycopg 3; SQLite with FTS5. Official client via httpx. OpenTelemetry API is a core dependency.

Provider extras pin Codex (`openai-codex`) and Claude (`claude-agent-sdk`). Grok, Cursor, OpenCode, and Prime Agent extras are markers for external executables.

Packaging uses uv. Versions are CalVer (`YYYY.M.PATCH`). Tests use pytest, pytest-django, pytest-asyncio, Hypothesis, and coverage gates.

## Related

- [System context](system-context.md)
- [Layered architecture](layered-architecture.md)
- [Development guidelines](../operations/development-guidelines.md)
