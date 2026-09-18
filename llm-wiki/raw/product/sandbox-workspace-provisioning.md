# Sandbox Workspace Provisioning Requirements

Product input approved on 2026-09-18.

## Intent

TalkToHarnesses owns project-environment creation and management inside its
sandboxes, for every client and for both Python and Node tooling. A client
never injects environment variables or setup commands to make a mounted
project usable; a repository declares its own setup and TTH runs it. The
split service's own runtime must be invisible to, and untouchable by, the
agents it launches.

Motivation: an agent working in a mounted worktree ran `uv run --python 3.13`,
and because the sandbox image exported `UV_PROJECT_ENVIRONMENT=/opt/venv`
(the split service's venv) into the agent's shell, uv deleted the service's
runtime and the sandbox stopped answering `/v1/health`.

## Provisioning

- Repo-declared only: a working directory's `.tth/setup.sh` is the single
  source of setup. There is no auto-detection of manifests and no API field
  for setup commands or environment variables. Repositories without the file
  get no provisioning.
- Runs before every session start or resume in that working directory, inside
  the kind's sandbox container as the service user, with the same mounts and
  limits as the harness, skipped while a stamp of the script, image and
  dependency manifests matches a successful run.
- A failing setup (non-zero exit, timeout, runner error) fails the session and
  the turn once, with a stable `workspace_setup_failed` error and a
  fixed-vocabulary reason; the turn is not retried.
- Setup progress and outcome are visible to clients as canonical
  `workspace_setup_started` / `workspace_setup_completed` events carrying a
  redacted, bounded output tail.
- Toolchains are downloaded on demand and cached on the per-kind `/data`
  volume: uv downloads the Python a project pins; Node 22 with npm and
  corepack (pnpm, yarn) ships in every image; caches live under `/data`.
- Hygiene: the split service's Python runtime is root-owned, off `PATH`, and
  no `UV_*` variable in the image references it; the harness child never
  inherits the split token or the split's Django settings module.

## Exclusions

- No `env` or `setup_commands` field on `HarnessConfiguration`.
- No detection-based provisioning (`uv.lock` or `package.json` alone trigger
  nothing).
- No warn-and-continue mode: a broken setup blocks the turn.

## Acceptance criteria

1. An agent's `uv sync` / `uv run` / `pip` / `npm` in a mounted project cannot
   alter the split service's runtime, and `/v1/health` stays healthy
   throughout a session that installs project dependencies.
2. A working directory with `.tth/setup.sh` is provisioned by the script
   before the harness starts there; a matching stamp skips the script; a
   changed script, manifest, or image runs it again.
3. A non-zero exit or timeout fails the session and turn with
   `workspace_setup_failed` and a reason (`exit_status`, `timeout`,
   `lock_timeout`, `runner_error`), without retry.
4. `workspace_setup_started` and `workspace_setup_completed` events bracket a
   run; nothing is emitted when there is no script or the stamp matches.
5. Every split image carries `uv`, Node 22, npm and corepack; interpreter and
   package caches persist on `/data` across sessions and container restarts.
6. The harness process environment carries neither `TTH_SPLIT_TOKEN` nor
   `DJANGO_SETTINGS_MODULE`.
