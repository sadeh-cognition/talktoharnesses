# Deploying talktoharnesses (tth-proxy) with split services

The `talktoharnesses` package is now **tth-proxy**: it owns the client-facing
API, persistence, auth, and orchestration. Each immutable project policy revision,
provider, and writable mount set gets an internal Docker network, a split
container, separate home/data volumes, and a credential gateway. Host provider
credentials stay in the gateway; the split receives scoped handles.

| Kind | Directory / image |
|---|---|
| grok | `tth-grok` |
| cursor | `tth-cursor` |
| codex | `tth-codex` |
| claude | `tth-claude` |
| opencode | `tth-opencode` |
| prime_agent | `tth-prime-agent` |
| muse | `tth-muse` |

All seven expose the identical HTTP+SSE API defined by the shared
[`tth-types`](../tth-types) package (`tth_types.split_api`).

## Building the split images

```sh
deploy/build-splits.sh                 # all seven in parallel, tag "latest"
deploy/build-splits.sh v1 claude muse  # some kinds, custom tag
```

The script wraps `docker buildx bake` (`docker-bake.hcl` at the repo root, one
matrix target per kind), which builds the requested kinds concurrently and
always builds `tth-policy-gateway` with the same tag. The gateway requires
Python 3.12 or newer; the proxy package still supports Python 3.11. Every
`tth-<kind>/Dockerfile` and `.dockerignore` is rendered from one template by
`scripts/render_dockerfiles.py` (the static gate runs it with `--check`), so
edit the template, not the generated files. The shared instructions come first
and nothing per-kind is set above them, so BuildKit reuses the apt, service-user
and uv layers across the splits that take the same branch (plain, Node, Cursor's
extra packages). Images embed the harness CLI (owned by the build UID/GID so the
split's executable-ownership check passes), install the locked Python
dependencies with `uv sync --frozen` into a **root-owned** `/opt/tth/venv`, and
run `/opt/tth/venv/bin/python -m uvicorn tth_<kind>.asgi:application` on
container port 8010. That venv is the service's alone: it is not on `PATH`,
not writable by the service user, and no `UV_*` variable in the image points at
it, so an agent running `uv sync`, `uv run` or `pip` in a mounted project gets
uv's ordinary behaviour (a `.venv` in the project) and cannot replace the
runtime that serves `/v1/health`. The per-kind layers are ordered by change
frequency (locked dependencies, tth-types, service source), so a source edit
rebuilds only the last thin layer.

Every image also ships the agent-facing toolchain: `uv`, Node 22 with `npm`,
and `corepack` (so `pnpm` and `yarn` resolve on first use). See
[Toolchains and caches](#toolchains-and-caches) for where their downloads go.

Pre-building is an optimization, not a requirement: the proxy builds a missing
`tth-<kind>` image itself on the first request for that kind (editable/repo
installs only — a wheel install has no build contexts on disk and fails with
`sandbox_unavailable` until the image is pre-built). To try an image without
touching a running sandbox, build it under a custom tag: the proxy replaces a
kind's container on its next preparation once a new image carries `latest`.

## Running the proxy

Managed project isolation requires a Linux Docker host.

Before upgrading, apply the proxy migrations and rebuild all split images plus
the gateway. Agentbahn must use the matching TTH and `tth-types` changes and run
its migrations as well. Existing conversations without a policy fail closed;
create a new harness/conversation bound to a project policy. Existing bound
conversations retain their original revision, while new ones use the latest.
Publication is disabled until an Agentbahn project administrator saves its
remote in the Sandbox settings.

Save a policy with `PUT /api/v1/sandbox-policies/{uuid}` using `policy` and
`expected_revision` (zero for creation). Read it with `GET` at the same route.
The authenticated owner controls revisions. Set the returned `ref` on the
harness configuration's `sandbox_policy` field. Concurrent stale saves fail.
Agentbahn's project Sandbox settings handle this lifecycle.

`ScopedSandboxManager` records scope identities in `talktoharnesses_sandbox`
and reuses compatible containers across proxy restarts. The gateway exposes an
ephemeral loopback port for host control; the split exposes no host port.
Startup/builds can return `sandbox_preparing`; retry shortly.

`TTH_SANDBOX_MOUNT_ROOTS` (default `$HOME/dev`) limits which host paths policies
may mount. Each scope mounts only its project, admitted linked worktrees and
Git common directory, plus explicitly declared read-only dependencies. Paths
keep their host locations. Gateway state and host credential paths cannot be
mounted into agents.

Tuning environment:

- `TTH_SANDBOX_MOUNT_ROOTS`: colon-separated operator-approved mount roots.
- `TTH_SANDBOX_IMAGE_TAG`: tag shared by split and gateway images.
- `TTH_SANDBOX_STATE_DIR`: private gateway state, default
  `$HOME/.local/state/talktoharnesses/sandboxes`; keep it outside project mounts.
- `TTH_SANDBOX_ENV_<KIND>`: host credential environment variable names. Values
  become provider-scoped handles in the agent. This is not arbitrary environment
  passthrough. The existing operator-only `TTH_SPLIT_ADAPTER_FACTORY` test hook
  remains available.
- `TTH_SANDBOX_<KIND>_AUTH_FILE`: host native credential file. Conventional
  defaults are `.grok/auth.json`, `.config/cursor/auth.json`,
  `.codex/auth.json`, `.claude/.credentials.json`,
  `.local/share/opencode/auth.json`, `.prime/config.json`, and `.config/muse/auth.json`.
  Log in on the host; do not log in inside agent containers. Unsupported native
  secret formats fail with `credential_proxy_unsupported`.

The gateway substitutes credentials only on admitted provider authentication
fields, rotates host refresh tokens under a file lock, verifies upstream TLS,
and denies private DNS results, arbitrary tunnels and Git receive-pack. The
sandbox cannot bypass it with direct network access. Muse uses its configurable
API URL to reach a fixed private reverse proxy because its native inference
transport does not trust the interception CA; the upstream origin remains fixed
and verified.

Defaults allow provider operations and read-only PyPI/npm downloads. Additional
HTTPS access requires exact host/path/method rules in the policy. Interpreter
downloads from GitHub need explicit rules for the release and asset hosts.
Private registries and secret-bearing MCP servers are not supported.

Runtime tuning (all optional):

- `TTH_RUNTIME_IDLE_REAP_SECONDS` — how long an idle conversation keeps its
  harness process before the proxy closes it (default 300). History and the
  native session are kept; the next turn resumes. Lower it when many
  short-lived conversations share one sandbox.
- `TTH_RUNTIME_MAX_RUNTIMES` — live harness processes per proxy worker
  (default 20). Beyond it new turns fail with `conversation_busy`
  ("runtime capacity reached") until a runtime is closed or reaped. Clients
  can release one early with `POST /conversations/{id}/runtime/close`. The
  close is refused with `409 conversation_busy` while a turn, background
  activity or harness switch is in flight. Runtimes are per worker and are
  not handed between workers, so with several proxy workers behind one
  address the close is also refused (same code, `reason:
  runtime_owned_by_other_worker`) when it lands on a worker that does not
  hold the conversation; retry, or let the idle reap release it.

The host control token is stored in the proxy database and differs from the
split token inside the scope. Gateway control authenticates the host token and
forwards only to that scope's split. Agents cannot use the host control route.

Containers drop all capabilities, disable privilege escalation, and run with
resource limits. The internal network has no host bridge address; direct
external traffic is blocked. Containers and their scope-specific volumes remain
available for resume until an operator removes them. Image/config drift
recreates containers on next use; schedule upgrades while conversations are idle.

## Toolchains and caches

Agents (and workspace setup scripts, below) work in bind-mounted projects with
plain `uv`, `node`, `npm`, `pnpm` and `yarn`. The proxy injects these variables
into every sandbox container so interpreter downloads and package caches land
on the persistent scope-specific `tth-scope-<id>-data` volume instead of the container's
writable layer or the project tree:

| Variable | Value |
|---|---|
| `UV_CACHE_DIR` | `/data/uv/cache` |
| `UV_PYTHON_INSTALL_DIR` | `/data/uv/python` (uv downloads the Python a project pins here) |
| `UV_LINK_MODE` | `copy` (projects and the cache sit on different filesystems) |
| `npm_config_cache` | `/data/npm/cache` |
| `npm_config_update_notifier` | `false` |
| `COREPACK_HOME` | `/data/corepack` |
| `COREPACK_ENABLE_DOWNLOAD_PROMPT` | `0` |
| `npm_config_store_dir` | `/data/pnpm/store` |
| `YARN_CACHE_FOLDER` | `/data/yarn/cache` |

They are managed like the split token: `TTH_SANDBOX_ENV_<KIND>` cannot override
them, and a container whose environment lacks them is recreated. Inspect what a
scope has cached with `docker exec tth-scope-<id> ls /data/uv/python /data/npm/cache`;
remove its data volume to start over.

The harness process itself never sees the split's own configuration: the split
moves `TTH_SPLIT_TOKEN` and `DJANGO_SETTINGS_MODULE` out of its environment once
Django is configured, so a Django project's `manage.py` inside the sandbox
resolves its own settings.

## Workspace setup

A repository declares how its environment is prepared in
**`.tth/setup.sh`** at the working directory's root. Nothing is detected or
inferred, and nothing about it crosses the API: TTH runs the file when it is
there and does nothing when it is not. A Python backend with a Node frontend
typically ships:

```sh
uv sync --frozen
(cd frontend && npm ci)
```

Contract:

- TTH runs `/bin/bash -e .tth/setup.sh` (no execute bit needed) with the
  working directory as cwd, as the service user, inside the kind's running
  container, before every session start or resume in that directory. The
  script sees the same mounts, resource limits and network as the harness.
- Environment: `PATH`, `HOME`, `LANG=C.UTF-8`, `TERM=dumb`, `USER=agent`,
  `TTH_WORKSPACE_SETUP=1`, `TTH_HARNESS_KIND=<kind>` and the toolchain
  variables above plus the gateway proxy and public CA settings. Provider credentials, the split token and the split's
  Django settings are never passed. stdin is closed; stdout and stderr are
  merged.
- It must be idempotent. TTH stamps a successful run (a digest of the script,
  the container image and the dependency manifests and lockfiles found in the
  working directory and one level down: `pyproject.toml`, `uv.lock`,
  `requirements.txt`, `package.json`, `package-lock.json`, `pnpm-lock.yaml`,
  `yarn.lock`, `.python-version`, `.nvmrc`, `Cargo.lock`, `go.sum`,
  `Gemfile.lock` and the like) under `/data/tth/workspaces/<key>/` and skips
  the script while the stamp matches. Editing any of those, rebuilding the
  image or a failed run makes the next session run it again.
- Exit status 0 is success. Anything else fails the session: the turn ends
  with `turn_failed` / `session_failed` carrying `workspace_setup_failed` and
  a reason (`exit_status`, `timeout`, `lock_timeout`, `runner_error`); the
  turn is not retried. The redacted last 4 KiB of output travel in the
  `workspace_setup_completed` event; the full output is in the proxy log and
  in `/data/tth/workspaces/<key>/setup.log` (capped at 4 MiB).
- `workspace_setup_started` and `workspace_setup_completed` events bracket a
  run so clients can show "preparing workspace"; a missing script or a
  matching stamp emits nothing.
- Concurrent sessions in the same directory and kind wait for each other
  (`asyncio` lock in the proxy, `flock` on the data volume). Different kinds
  do not serialize; keep the script safe to run twice.

Tuning:

- `TTH_WORKSPACE_SETUP=0` disables workspace setup entirely (operator kill
  switch; scripts are ignored).
- `TTH_WORKSPACE_SETUP_TIMEOUT` — seconds a run may take before its process
  group is killed (default 900).

## RTK command rewriting

[RTK](https://github.com/rtk-ai/rtk) rewrites shell commands to `rtk <cmd>`
so the harness reads a token-trimmed version of the output. The pinned
release binary (`RTK_VERSION` build arg) is installed in the claude, codex,
cursor, opencode, grok, and muse images. prime_agent runs shell commands
through its `ipython` tool (`%%bash` cells) rather than Pi's `bash` tool, so
RTK's Pi extension never sees them; that image is untouched. Integration
differs per kind:

| Kind | Mechanism | Where it lives |
|------|-----------|----------------|
| claude | in-process SDK `PreToolUse` hook calling `rtk hook claude` | `tth_claude.harness.rtk_hook` (the split runs with `setting_sources=[]`, so no settings.json) |
| cursor | `preToolUse` hook (`rtk hook cursor`) | `/home/agent/.cursor/hooks.json`, seeded |
| opencode | plugin calling `rtk rewrite` | `/home/agent/.config/opencode/plugins/rtk.ts`, seeded |
| codex, muse | RTK's Codex rules file (prompt-level; the model follows it) | `/home/agent/.codex/AGENTS.md`, seeded with the `RTK.md` rules inlined (Codex does not expand the `@file` reference `rtk init` writes; Muse loads the file as compatible personal rules) |
| grok | the same rules file, in Grok's global rules location | `/home/agent/.grok/AGENTS.md`, seeded with the same inlined rules |

RTK has no hook for Codex, Grok, or Muse, so those kinds get RTK's own Codex
rules text in a file their system prompt already includes; the split adapters
and the canonical TTH prompt are untouched. Muse reads `~/.codex/AGENTS.md`
unless its `context.foreign_personal_rules` setting is off.

"Seeded" files are written by `rtk init --global …` in a one-shot container
against the kind's home volume (no network, all capabilities dropped) every
time the sandbox is prepared, right after credential seeding; seeding is
idempotent and this also upgrades existing homes after an image bump. Seeding
fails open when `rtk` is missing or errors. Claude's separate command guard
checks the final rewritten Bash command through the gateway and fails closed,
even with yolo enabled. Other providers are checked only when they emit command
approval requests. The UI reports this coverage: the blocklist cannot prevent
execution through unobserved tools or arbitrary scripts.

Confirm inside a prepared sandbox:

```sh
docker exec <codex-scope-container> rtk --version
docker exec <codex-scope-container> cat /home/agent/.codex/AGENTS.md
docker exec <grok-scope-container> cat /home/agent/.grok/AGENTS.md
docker exec <cursor-scope-container> cat /home/agent/.cursor/hooks.json
docker exec <claude-scope-container> rtk gain --history   # rewrites recorded so far
```

## OpenTelemetry

The proxy retains its existing OTLP configuration. Managed agent containers
set `OTEL_SDK_DISABLED=true`; host collector endpoints and secret headers are
not forwarded into the sandbox. The shared split telemetry implementation honors
this flag. Directly deployed splits retain their existing telemetry configuration.

## Live gates

The docker sandbox path has an opt-in gate: `TALKTOHARNESSES_SANDBOX_DOCKER=1`
runs `tests/live/test_sandbox_docker.py`.

Workspace setup has its own credential-free gate:
`TALKTOHARNESSES_SANDBOX_WORKSPACE=1` runs
`tests/live/test_sandbox_workspace_live.py`, which boots a throwaway container
(kind `claude` by default, `TALKTOHARNESSES_SANDBOX_WORKSPACE_KIND` to change
it; the image must be built), provisions a repository that pins Python 3.13 and
carries a `frontend/package.json` through `.tth/setup.sh`, and checks the
stamp, skip, failure, cache and hygiene contracts (`/opt/tth/venv` stays
read-only and `/v1/health` stays 200). It downloads a Python and an npm
package, so it needs network access.

Every kind has a sandbox live gate: `tests/live/test_<kind>_sandbox_live.py`,
enabled with `TALKTOHARNESSES_LIVE_<KIND>_SANDBOX=1`. The fixture selects the
sandbox policy/provider, assigns an isolated scope, and cleans them up
afterward. The full create/resume journey uses the official TTH HTTP client
while TTH handles workspace mounting, gateway control, managed container startup, and
credential proxying:

```sh
TALKTOHARNESSES_LIVE_GROK_SANDBOX=1 \
  uv run pytest tests/live/test_grok_sandbox_live.py -q -s --tb=short
```

Each sandbox gate specifically requires the kind's host credential file (see
the auth-file table above) and removes the kind's API-key env var, proving
that seeded host-file authentication works on its own. A missing
`tth-<kind>:latest` image is built on demand; pre-build it to keep the gate
fast. OpenCode's
host file is created by `opencode auth login`. Native host logins are virtualized
on every scope preparation, and the gateway reloads credentials for requests.

A focused login compatibility gate creates and resumes a conversation for each
provider without requiring unrelated tool features:

```sh
TTH_SANDBOX_IMAGE_TAG=latest TALKTOHARNESSES_POLICY_PROVIDERS=1 \
  uv run --extra django --extra client --extra gateway pytest \
  tests/live/test_policy_provider_sessions.py -q
```
