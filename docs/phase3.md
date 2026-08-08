# Phase 3 — Process and Runtime Supervision

## Summary

- Merge the completed Phase 2 branch first, then implement Phase 3 as version 2026.8.0.dev3.
- Add a Django-free talktoharnesses.runtime package providing secure process supervision and lifecycle-only runtime management.
- Persist every launch and process/session lifecycle change. Command delivery and provider-event normalization remain Phase 4 work.

## Public Contracts

- Export ProcessSpec, ProcessHandle, ProcessEvent, RuntimePolicy, ProcessSupervisor, and RuntimeManager from talktoharnesses.runtime.
- Define frozen ProcessSpec with conversation/binding IDs, immutable LaunchSnapshot, and adapter-constructed arguments excluding the executable.
    It exposes no shell string, environment override, or directory-creation option.

- ProcessHandle exposes async stdin writes, a single-consumer raw stdout byte stream, lifecycle events, the current redacted stderr tail, wait,
    interrupt, close, and forced termination without exposing the underlying platform process object.

- Define process events for start, one-time stderr truncation, silence warning, exit, and forced termination. Stdout bytes are never represented
    as diagnostic events.

- Add frozen RuntimePolicy defaults: 10-second creation, 60-second start/resume, 15-minute idle reap, 2-minute silence warning, 5-second
    interrupt and graceful-close calls, 2-second terminate escalation, and a 10-second total shutdown budget.

- Make StartSessionRequest.launch and ResumeSessionRequest.launch required.
- Change AdapterRegistry to register(kind, factory) and create(kind), where the factory returns a new HarnessAdapter. Remove instance-returning
    get rather than retaining a compatibility stub.

- Extend Persistence with one coarse commit_runtime_lifecycle(...) operation that atomically updates the aggregate, process record/redacted tail,
    immutable launch history, and canonical lifecycle events using Phase 2 sequence allocation.

## Implementation Changes

- Resolve executable, working-directory, and workspace-root symlinks strictly without creating paths. Require directories for roots and a regular
    executable file; use effective UID ownership and execute bits on Unix, and current-token/file-owner SID comparison on Windows. Reuse the
    existing missing-directory errors and add only invalid_executable, executable_owner_mismatch, and runtime_timeout.

- Build each LaunchSnapshot from the resolved paths, probed capabilities, model/mode, and adapter version. Store it as both the binding’s latest
    snapshot and an append-only launch-history entry; retries remain idempotent by process-incarnation UUID.

- Launch the resolved executable plus the adapter’s argument tuple directly:
  - Unix: asyncio.create_subprocess_exec(..., start_new_session=True) and process-group SIGINT, SIGTERM, then SIGKILL.
  - Windows: pin pywin32==312 and matching types-pywin32 stubs, create the process suspended with a new process group, attach it to a
        kill-on-close Job Object, then resume it. Closing or terminating the job kills its descendants, as defined by Microsoft’s Job Object
        behavior (<https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects>); new-process-group control events follow the Python
        subprocess contract (<https://docs.python.org/3.11/library/subprocess.html>). Build 312 supports Python 3.11 and upstream recommends exact
        pins for its incrementing API versions (pywin32 on PyPI (<https://pypi.org/project/pywin32/>)).

- Reserve stdout as an opaque byte stream for Phase 4 framing. Read stderr concurrently through Phase 2’s centralized streaming text redactor,
    retain the newest 10 MiB of valid UTF-8, expose the live tail, and persist it at truncation and terminal lifecycle checkpoints. Emit
    ProcessStderrTruncatedPayload exactly once per process UUID.

- Treat two minutes without stdout activity as one warning episode. Persist ProviderWarningPayload(code="provider_silence"); do not change turn/
    session status, settle commands, or stop the process. New stdout resets the warning episode.

- Record STARTING before spawn, RUNNING with PID after spawn, and terminal status/exit code afterward. A timed-out spawn that completes late must
    be immediately tree-terminated. Map abnormal exits to ProcessExitedPayload plus SessionFailedPayload; map escalation to
    ProcessForcedTerminationPayload.

- Implement session start/resume/close/failure transitions that update the binding’s native ID/latest snapshot and emit existing canonical
    events. Any persistence conflict or session startup failure closes the newly created runtime so no unrecorded process survives.

- Implement RuntimeManager with a per-conversation lock and one managed runtime entry. It creates a fresh adapter through the registry, registers
    the runtime only after successful start/resume, and removes it only after adapter tasks and supervised processes are closed.

- Reconcile idle timers against the authoritative persisted snapshot. At expiry, re-read state and reap only when idle_reap_eligible remains
    true; running background activities or queued/active work cancel reaping. Reaping closes live resources and commits reap_session while
    preserving the native resume ID and every launch record.

- A normal interrupt gets five seconds for the adapter call; a hung call closes that runtime through process escalation. Provider acknowledgement
    and turn settlement remain Phase 4 responsibilities.

- Shutdown is idempotent: reject new runtimes, interrupt active conversations concurrently, use the remaining shared ten-second budget for
    graceful completion/close, then force-terminate all remaining process groups/jobs and cancel owned tasks.

## Test Plan

- Add deterministic helper-process modes for malformed stdout, split output, large/redaction-sensitive stderr, silence, startup hangs, ignored
    interrupts, descendant creation, and chosen abnormal exit codes. Copy the test interpreter into a temporary owned executable so security checks
    remain real.

- Verify symlink resolution, non-created directories, missing/non-directory roots, invalid executables, mocked ownership mismatch, argv
    preservation, no shell invocation, and immutable launch snapshots.

- Verify malformed stdout is forwarded byte-for-byte and never enters stderr, logs, or lifecycle payloads; stderr never enters stdout; retained
    stderr is UTF-8-safe and at most 10 MiB; secrets split across reads are redacted; and exactly one truncation event is committed.

- Test every timeout with short injected policy values and event synchronization: creation cleanup, start/resume cleanup, silence warning/reset,
    interrupt timeout, graceful close, terminate escalation, and the ten-second shutdown ceiling.

- Test concurrent creation for one conversation, distinct adapter instances across conversations, fresh instances after reap/resume, background-
    activity suppression, native-ID/history retention, abnormal exits, optimistic lifecycle conflicts, and repeated start/interrupt/reap/close
    cycles without child processes or asyncio tasks leaking.

- Run real process-tree termination tests in the existing Linux, macOS, and Windows CI matrix. Run lifecycle persistence assertions against the
    Phase 2 SQLite/PostgreSQL contract suite.

- Gate with Ruff, format check, strict Pyright, full tests, lockfile check, migrations check, wheel/sdist builds, and imports proving
    talktoharnesses.runtime remains Django-free.

## Assumptions

- The separate Phase 2 branch supplies DjangoPersistence, process/launch tables, transaction-safe event allocation, and the centralized redactor
    described in the accepted plan; the current checkout is not the implementation baseline until that branch is merged.

- Child processes inherit the supervisor’s environment unchanged; configurable environment mutation is deferred until a concrete adapter requires
    it.

- Phase 3 does not add protocol framing, command workers, provider schemas, generic event sinks, API endpoints, synchronous wrappers, or concrete
    harness adapters.
