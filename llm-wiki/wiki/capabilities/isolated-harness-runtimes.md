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
last_verified: 2026-08-30
verified_against_commit: 2920f5820783245bd5b871e61edd44642ebfe56a
---

# Isolated Harness Runtimes

Each active conversation owns one supervised SDK or process runtime. Request handlers never own its lifetime. Disconnecting the last client does not interrupt work.

## Product value

Turns outlive HTTP requests. Harness execution occurs in the kind's split service, which runs in a restricted proxy-managed Docker container with explicitly mounted workspace access.

## Current implementation

The proxy's `RuntimeManager` mirrors split sessions through `RemoteProcessHandle`; each split's `ProcessSupervisor` creates, watches, and reaps the native runtime. SQLite uses a single-supervisor proxy profile. PostgreSQL may coordinate multiple proxy workers through transactional claims, renewable leases, and notifications without transferring a live split session.

Split images for claude, codex, cursor, and opencode also carry the RTK command rewriter (`rtk`), which prefixes shell commands so the harness reads trimmed output. Claude applies it as an in-process SDK `PreToolUse` hook (`tth_claude.harness.rtk_hook`); the proxy seeds the cursor hook, opencode plugin, and codex rules file into the kind's home volume with `rtk init` during sandbox preparation (`talktoharnesses.remote.sandbox_rtk`). Seeding fails open. grok and muse are not supported by RTK, and prime_agent's `ipython` shell tool is invisible to RTK's Pi extension, so those images are untouched.

## Requirements

- [Host Django ASGI with readiness](../requirements/host-django-asgi-with-readiness.md)
- [Submit turns and stream events](../requirements/submit-turns-and-stream-events.md)

## Related

- [Runtime isolation architecture](../architecture/runtime-isolation.md)
- [Runtime isolation decision](../decisions/runtime-isolation.md)
- [System context](../architecture/system-context.md)
