---
type: domain
title: Harness Instance
status: implemented
audiences:
  - product
  - developer
tags:
  - type/domain
  - capability/adapters
last_verified: 2026-08-21
verified_against_commit: 3f90f85a37028a1ba0498cff641ef5c8a1bec6d7
---

# Harness Instance

A harness instance is an owner-owned named configuration: kind, working directory, workspace roots, and optional model, mode, effort, and yolo. Create and stored configuration reject executable paths. Process-bound kinds locate their conventional CLI at probe and launch; Codex and Claude use bundled SDKs. A `LaunchSnapshot` records the resolved executable that actually ran.

Probe produces `HarnessCapabilities` and a `VersionAdvisory`. Switching harnesses creates a new binding rather than mutating the old one.

## Related

- [Probe and configure harnesses](../requirements/probe-and-configure-harnesses.md)
- [Conversation](conversation.md)
- [Unified harness adapters](../capabilities/unified-harness-adapters.md)
