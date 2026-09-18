---
type: requirement
title: Provision Sandbox Workspaces
status: implemented
audiences:
  - product
  - developer
tags:
  - type/requirement
  - capability/runtime
  - status/implemented
last_verified: 2026-09-18
verified_against_commit: f7e2c5e25226f669f969fe6ff5fbf25b8af5fa96
sources:
  - raw/product/sandbox-workspace-provisioning.md
  - raw/product/on-demand-sandbox-provisioning.md
---

# Provision Sandbox Workspaces

## Intent

A repository declares how its environment is prepared, and TalkToHarnesses runs that preparation inside the kind's sandbox before a harness works there. Clients inject nothing; the split service's own runtime stays invisible to agents; Python and Node toolchains download on demand into persistent caches.

## Current behavior

Every split image is rendered from `scripts/render_dockerfiles.py` with a root-owned service venv at `/opt/tth/venv` that is off `PATH`, not writable by the service user, and referenced only by the `CMD`. The image exports no `UV_*` variable. Agents get plain `uv`, Node 22, `npm` and `corepack` in every kind. `SandboxManager` injects the toolchain cache variables (`UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR`, `UV_LINK_MODE`, npm, corepack, pnpm and yarn cache locations under `/data`) into every container as managed environment: operator passthrough cannot override them and a container lacking them is recreated.

Each split moves `TTH_SPLIT_TOKEN` and `DJANGO_SETTINGS_MODULE` out of `os.environ` once Django is configured (`tth_<kind>.shared.private_env.seal`), so neither the supervised process nor an SDK-spawned CLI inherits them.

Before `RuntimeManager` opens any split session (a client start or resume, a recovery resume after a worker crash, or a candidate runtime for a harness switch) it calls the remote adapter's `prepare_workspace` hook through one shared method. The `SandboxManager` runs `/bin/bash -e .tth/setup.sh` from the working directory inside the kind's running container through `docker exec`, as the service user, with a whitelist environment (`PATH`, `HOME`, `LANG`, `TERM`, `USER=agent`, `TTH_WORKSPACE_SETUP=1`, `TTH_HARNESS_KIND` and the toolchain variables). The in-container runner (`talktoharnesses.remote.workspace_runner`, shipped as source on the exec command line; its Unix-only lock import is deferred so the proxy package imports on Windows hosts) takes a per-directory `flock` under `/data/tth/workspaces/<key>/`, compares a stamp (script bytes, container image id, dependency manifests and lockfiles at the root and one level down) with the last successful run, streams the merged output back, on timeout (`TTH_WORKSPACE_SETUP_TIMEOUT`, default 900 s) sends SIGTERM to the process group and SIGKILLs whatever is still in it after the grace period whether or not the shell already exited, and writes the post-run stamp only on exit 0. The proxy keeps only a rolling 4 KiB tail of the output while it streams and logs it line by line; the full log is the runner's `setup.log` in the state directory. A missing script or a matching stamp runs nothing.

The manager commits `workspace_setup_started` while the script runs and `workspace_setup_completed` (status `succeeded`, `failed` or `timed_out`, exit code, duration and a redacted 4 KiB output tail) before `session_started`, `session_resumed` or `session_failed`. One recorder (`runtime/workspace_setup.py`) owns those writes and commits them without a process row, so no setup event can change the process status; it retries optimistic conflicts so a concurrent client write cannot turn a setup failure into a retryable persistence error, and once the setup await is cancelled (shutdown) or has returned, a progress callback still arriving from the docker exec thread is dropped instead of reviving a failed process record. A failure raises `workspace_setup_failed` with a fixed-vocabulary reason (`exit_status`, `timeout`, `lock_timeout`, `runner_error`); the command processor settles the turn with `turn_failed` without retrying, and the HTTP layer maps the code to 409. `TTH_WORKSPACE_SETUP=0` disables the feature. Candidate runtimes for harness switches run setup without lifecycle events. A recovery resume runs setup like a client resume: a matching stamp runs nothing, and a setup failure rejects the native resume so the coordinator falls back.

## Gap

Concurrent setups of one directory by different kinds are not serialized (the lock lives on the per-kind data volume). Per-kind `/data` volumes duplicate toolchain caches across kinds. A cancelled setup await cannot stop the docker exec already running in the container; the script finishes on its own and only its reporting is suppressed.

## Acceptance criteria

- An agent's `uv`, `pip` or `npm` in a mounted project cannot alter the split service's runtime; `/v1/health` stays healthy across a session that installs project dependencies.
- A working directory with `.tth/setup.sh` is provisioned before the harness starts or resumes there, including recovery resumes and candidate runtimes; a matching stamp skips the script; a changed script, manifest or image runs it again.
- A non-zero exit or timeout fails the session and the turn with `workspace_setup_failed` and a reason, without retry, even when persisting the outcome hits an optimistic conflict.
- A timed-out script leaves no process from its group running, even when the shell exited on SIGTERM before its children.
- A setup progress event that arrives after the session was cancelled and failed is not recorded and does not change the process record.
- `workspace_setup_started` and `workspace_setup_completed` bracket a run; nothing is emitted for a missing script or a matching stamp.
- Every split image carries `uv`, Node 22, `npm` and `corepack`; interpreter and package caches persist on `/data`.
- The harness process environment carries neither `TTH_SPLIT_TOKEN` nor `DJANGO_SETTINGS_MODULE`.

## Implementation evidence

- `scripts/render_dockerfiles.py` (`SERVICE_VENV`, `_uv_sync`, Node and corepack in the common prefix)
- `src/talktoharnesses/remote/sandbox_workspace.py` (`TOOLCHAIN_ENV`, `run_setup`, `WorkspaceSetupFailed`)
- `src/talktoharnesses/remote/workspace_runner.py`
- `src/talktoharnesses/remote/sandbox.py` (`SandboxConfig.workspace_setup_*`, `SandboxManager.prepare_workspace`, `_environment`, `_container_matches`)
- `src/talktoharnesses/remote/adapter.py` (`WorkspaceSetupProvider`, `RemoteHarnessAdapter.prepare_workspace`)
- `src/talktoharnesses/runtime/workspace_setup.py` (`prepare_workspace`, `WorkspaceSetupRecorder`; called from the three session-opening paths in `runtime/manager.py`)
- `tth-types/src/tth_types/enums.py`, `tth-types/src/tth_types/errors.py`, `tth-types/src/tth_types/events.py` (`WORKSPACE_SETUP_FAILED`, `WorkspaceSetupStartedPayload`, `WorkspaceSetupCompletedPayload`)
- `tth-*/src/tth_*/shared/private_env.py`, `tth-*/src/tth_*/asgi.py`, `tth-*/src/tth_*/auth.py`
- `deploy/README.md` (Toolchains and caches, Workspace setup)

## Test evidence

- `tests/test_render_dockerfiles.py`
- `tests/unit/remote/test_workspace_runner.py`
- `tests/unit/remote/test_sandbox_workspace.py`
- `tests/unit/remote/test_sandbox_and_registry.py` (toolchain environment, `prepare_workspace`)
- `tests/runtime/test_workspace_setup.py` (events, failure, conflict retry, cancellation, candidates, recovery)
- `tests/unit/application/test_command_processor.py` (`test_startup_error_settles_command_instead_of_retrying`)
- `tth-*/tests/test_private_env.py`
- `tests/live/test_sandbox_workspace_live.py` (`TALKTOHARNESSES_SANDBOX_WORKSPACE=1`)

## Related

- [Isolated harness runtimes](../capabilities/isolated-harness-runtimes.md)
- [Sandbox toolchain hygiene decision](../decisions/sandbox-toolchain-hygiene.md)
- [Split services decision](../decisions/split-services.md)
- [Probe and configure harnesses](probe-and-configure-harnesses.md)
- [Deployment](../operations/deployment.md)
- [Approved sandbox workspace provisioning](../../raw/product/sandbox-workspace-provisioning.md)
