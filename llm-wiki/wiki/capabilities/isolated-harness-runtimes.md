---
type: capability
title: Isolated Harness Runtimes
status: implemented
audiences:
  - product
  - developer
tags:
  - type/capability
  - capability/runtime
  - status/implemented
last_verified: 2026-09-19
verified_against_commit: 95f006ecec870bd6b22549fd96755f723776970e
---

# Isolated Harness Runtimes

Each active conversation owns one supervised SDK or process runtime. Request handlers never own its lifetime. Disconnecting the last client does not interrupt work.

## Product value

Turns outlive HTTP requests. Harness execution occurs in the kind's split service, which runs in a restricted proxy-managed Docker container with explicitly mounted workspace access.

## Current implementation

The proxy's `RuntimeManager` mirrors split sessions through `RemoteProcessHandle`; each split's `ProcessSupervisor` creates, watches, and reaps the native runtime. SQLite uses a single-supervisor proxy profile. PostgreSQL may coordinate multiple proxy workers through transactional claims, renewable leases, and notifications without transferring a live split session.

Split images for claude, codex, cursor, opencode, grok, and muse carry RTK (`rtk`) for filtered shell output. Claude applies it as an in-process SDK `PreToolUse` hook (`tth_claude.harness.rtk_hook`); the proxy seeds every other integration into the kind's home volume with `rtk init` during sandbox preparation (`talktoharnesses.remote.sandbox_rtk`): the cursor hook, the opencode plugin, and RTK's Codex rules file for codex, grok, and muse, which have no RTK hook and follow the rules from their system prompt (Codex reads `~/.codex/AGENTS.md`, Grok its global `~/.grok/AGENTS.md`, Muse `~/.codex/AGENTS.md` as compatible personal rules). Split adapters and the canonical prompt are untouched. Seeding is idempotent across re-preparation and fails open; `tests/unit/remote/test_sandbox_and_registry.py` covers the seeded script, and `scripts/render_dockerfiles.py` owns the shared installation block. prime_agent's `ipython` shell tool is invisible to RTK's Pi extension, so that image remains unchanged.

Every image carries `uv`, Node 22, `npm` and `corepack` for agents working in mounted projects, with interpreter and package caches on the per-kind `/data` volume; the split service's own venv is root-owned, off `PATH` and never referenced by an exported `UV_*` variable, and the harness child inherits neither the split token nor the split's Django settings module. Before a session starts or resumes in a working directory, the proxy runs that directory's repo-declared `.tth/setup.sh` inside the sandbox (stamped, locked, timed out, and reported through `workspace_setup_started` / `workspace_setup_completed`); a failing setup fails the turn with `workspace_setup_failed`. See [Provision sandbox workspaces](../requirements/provision-sandbox-workspaces.md).

## Requirements

- [Host Django ASGI with readiness](../requirements/host-django-asgi-with-readiness.md)
- [Submit turns and stream events](../requirements/submit-turns-and-stream-events.md)
- [Provision sandbox workspaces](../requirements/provision-sandbox-workspaces.md)

## Related

- [Runtime isolation architecture](../architecture/runtime-isolation.md)
- [Runtime isolation decision](../decisions/runtime-isolation.md)
- [Sandbox toolchain hygiene decision](../decisions/sandbox-toolchain-hygiene.md)
- [System context](../architecture/system-context.md)
