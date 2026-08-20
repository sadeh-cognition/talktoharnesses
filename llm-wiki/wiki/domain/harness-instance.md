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
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Harness Instance

A harness instance is an owner-owned named configuration: kind, working directory, optional executable path, model, mode, effort, yolo, and workspace roots.

Probe produces `HarnessCapabilities` and a `VersionAdvisory`. A `LaunchSnapshot` records the resolved executable, versions, and capabilities used to start or resume a binding. Switching harnesses creates a new binding rather than mutating the old one.

## Related

- [Probe and configure harnesses](../requirements/probe-and-configure-harnesses.md)
- [Conversation](conversation.md)
- [Unified harness adapters](../capabilities/unified-harness-adapters.md)
