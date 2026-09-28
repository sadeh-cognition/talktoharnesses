# ADR 0008: Split Session Reattach After a Proxy Restart

- **Status:** Accepted
- **Date:** 2026-09-27

## Context

A split runs in a sandbox container that usually outlives a proxy restart. The
split used to close a session as soon as the proxy's event stream dropped, so
a crash or SIGKILL of the proxy killed the harness turn in flight, and recovery
could only native-resume a fresh session and fail that turn
(`turn_lost_on_restart`). Work already paid for was lost.

## Decision

- A dropped event stream detaches the split session instead of closing it. The
  harness keeps running and the split retains the frames it sent. A session
  left detached longer than `TTH_SPLIT_DETACH_GRACE_SECONDS` (default 120; 0
  keeps the old close-at-once behaviour) is closed.
- Frame ids are assigned when a frame is queued. `GET
  /v1/sessions/{sid}/events?after=<id>` reattaches the single subscriber and
  replays the retained frames after that id; a cursor the split can no longer
  serve answers `invalid_cursor`.
- The proxy commits a `SplitStreamCursor` (binding, split session, last frame)
  in the conversation state with every event batch, so the cursor always
  matches the committed events.
- Recovery classifies a turn in flight with a cursor for the current binding
  as `reattach`: only an already running sandbox is used, and the reattach
  opens the replay stream from the cursor itself, so the split's own cursor
  check decides (reason `session_reattached`). A missing session or an
  `invalid_cursor` fails the reattach, which closes that split session and
  classifies again without it. A turn in flight that cannot be reattached is
  settled as `outcome_unknown` with reason `turn_lost_on_restart` instead of
  being native-resumed: a fresh split session never continues a turn begun
  before it, so resuming would only leave the turn running with nothing
  behind it. The next turn resumes natively as usual.

## Consequences

This revises the Phase 9 non-goal that a live split stream is never adopted:
the proxy adopts the split *session* (not a process or PID), and only through
the committed cursor. Graceful proxy shutdown still closes sessions and settles
in-flight turns. A split restart still loses its sessions. Reattach is
per-process like the rest of SQLite recovery: it is attempted by the worker
that takes over the conversation.
