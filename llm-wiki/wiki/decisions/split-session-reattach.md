---
type: decision
title: Split Session Reattach Decision
status: implemented
audiences:
  - developer
tags:
  - type/decision
  - audience/developer
last_verified: 2026-09-28
verified_against_commit: 247a3b0694694a966bb9b3e690d30c69a2612c2c
sources:
  - raw/engineering/adr-0008-split-session-reattach.md
---

# Split Session Reattach Decision

A split's sandbox container usually outlives a proxy restart, so a dropped proxy event stream detaches the split session instead of closing it. The harness keeps running and the split retains the frames it sent. A session detached longer than `TTH_SPLIT_DETACH_GRACE_SECONDS` (default 120; 0 closes at once) is closed.

Frame ids are assigned when a frame is queued. `GET /v1/sessions/{sid}/events?after=<id>` reattaches the single subscriber and replays the retained frames after that id; a cursor the split can no longer serve answers `invalid_cursor`. The proxy commits a `SplitStreamCursor` (binding, split session, last frame) in the conversation state with every event batch, so the cursor always matches the committed events.

Recovery classifies a turn in flight with a cursor for the current binding as `reattach` (reason `session_reattached`), using only an already running sandbox. A missing session or `invalid_cursor` fails the reattach, closes that split session, and classifies again without it. A turn in flight that cannot be reattached is settled as `outcome_unknown` with reason `turn_lost_on_restart` instead of being native-resumed, because a fresh split session never continues a turn begun before it. The next turn resumes natively.

This revises the Phase 9 non-goal that a live split stream is never adopted: the proxy adopts the split session, not a process, and only through the committed cursor. Graceful proxy shutdown still closes sessions and settles in-flight turns, and a split restart still loses its sessions.

Implementation evidence: `src/talktoharnesses/domain/models.py` (`SplitStreamCursor`), `src/talktoharnesses/domain/transitions.py` (`ConversationState.split_stream`), `src/talktoharnesses/application/event_dispatcher.py`, `src/talktoharnesses/application/recovery.py`, `src/talktoharnesses/application/worker_coordinator.py`, `src/talktoharnesses/remote/adapter.py`, `src/talktoharnesses/runtime/manager.py`, `tth-types/src/tth_types/split_api.py`, and each split's `sessions.py` and `sse.py`. Test evidence: `tests/unit/application/test_recovery_classifier.py`, `tests/unit/application/test_event_dispatcher.py`, `tests/unit/application/test_worker_coordinator.py`, `tests/unit/remote/test_remote_adapter.py`, `tests/runtime/test_runtime_manager.py`, and each split's vendored `tests/test_split_sessions.py`.

## Related

- [ADR 0008 source](../../raw/engineering/adr-0008-split-session-reattach.md)
- [Split services decision](split-services.md)
- [Runtime isolation decision](runtime-isolation.md)
- [Runtime isolation architecture](../architecture/runtime-isolation.md)
- [Adapter protocol](../interfaces/adapter-protocol.md)
