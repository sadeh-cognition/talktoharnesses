---
type: interface
title: Adapter Protocol
status: implemented
audiences:
  - developer
tags:
  - type/interface
  - capability/adapters
  - status/implemented
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Adapter Protocol

`HarnessAdapter` is a fixed asynchronous protocol: `probe`, `start`, `resume`, `submit`, `steer`, `interrupt`, `answer_interaction`, `events`, and `close`. Request and session types are frozen Pydantic models with no live process objects.

`HarnessSession` is an opaque handle (`conversation_id`, `binding_id`, `kind`, `native_session_id`, model/mode/effort, metadata). Interaction requests carry a canonical payload plus provider correlation.

Provider-specific types must not leak through this protocol.

## Related

- [Unified harness adapters](../capabilities/unified-harness-adapters.md)
- [Provider adapters](../architecture/provider-adapters.md)
- [Steer, interrupt, and switch harness](../requirements/steer-interrupt-and-switch-harness.md)
