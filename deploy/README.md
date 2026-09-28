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
rebuilds only the last thin layer. The `uv sync` steps use a throwaway uv
cache, never a BuildKit cache mount: policy image instructions (see
[Custom sandbox images](#custom-sandbox-images)) build on the same BuildKit,
and nothing they leave behind may reach a harness image. A rebuild after a
`uv.lock` change downloads the locked dependencies again.

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

Each denial appears in the scope gateway's log (`docker logs
<scope>-gateway`) as `sandbox_policy_denied` with the policy id, revision,
reason and host. `egress_denied` means the host is not allowed, `port_denied`
an allowed host reached other than over HTTPS on port 443, and
`private_address` an allowed host whose DNS answers include a non-public
address. Paths, query strings, bodies and headers are never logged.

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
  `?release_sandbox=true` also stops the conversation's sandbox scope: its
  containers and network go unless another runtime in this process uses
  them. This works even after the idle reap closed the runtime. The volumes
  stay, so the conversation still resumes; the next session recreates the
  containers. A stop cannot see another proxy process using the same state
  root, so with several processes it removes that process's containers too.
  The Python client exposes it as `close_runtime(..., release_sandbox=True)`.

The host control token is stored in the proxy database and differs from the
split token inside the scope. Gateway control authenticates the host token and
forwards only to that scope's split. Agents cannot use the host control route.

Containers drop all capabilities, disable privilege escalation, and run with
resource limits. The internal network has no host bridge address; direct
external traffic is blocked. Image/config drift recreates containers on next
use; schedule upgrades while conversations are idle.

Every mount set gets its own scope, so each linked worktree (for example one
per Agentbahn workflow run) adds a scope. The proxy reclaims them in two tiers:

- Containers and the internal network are disposable: they are removed once
  the scope has been unused for `TTH_SANDBOX_CONTAINER_IDLE_SECONDS` (default
  86400), or at the next pass after they stop running, e.g. after a Docker
  Desktop restart left them unstartable. The next session in that scope
  recreates them.
- The `-home`/`-data` volumes, the private state directory and the sandbox row
  keep harness sessions and caches, so old conversations still resume
  natively. They are purged when a host path the scope mounts no longer exists
  (its worktree was deleted), or after `TTH_SANDBOX_PURGE_IDLE_SECONDS`
  (default 7776000, 90 days; `0` keeps them).

Each scope needs its own Docker network, and Docker's default address pools
allow only about 30 bridge networks. Keep the container idle period short, or
close finished runs with `release_sandbox`, when many worktrees get a scope;
otherwise preparation fails with `sandbox_unavailable` (reason
`network_pool_exhausted`).
A longer-term fix is a wider `default-address-pools` in the Docker daemon
settings, for example `{"base": "10.200.0.0/16", "size": 24}`.

Scopes with a live harness runtime in this process are never reclaimed. The
reaper passes every `TTH_SANDBOX_REAP_INTERVAL_SECONDS` (default 600, and
shorter than the container idle period), first 30 seconds after startup.
`TTH_SANDBOX_REAPER=0` stops it reclaiming; passes still mark the scopes this
process uses. It only considers scopes whose state directory is under this
proxy's `TTH_SANDBOX_STATE_DIR`, so legacy `tth-<kind>` containers and scopes
prepared under another state root (a second proxy, or a live test's temporary
one) are never touched. With several proxy processes sharing a state root, a
scope used only by another process is judged by its `last-used` state file.
Processes touch it when a runtime binds to or leaves the scope, and on every
pass while it is in use. Per-scope lock files live in the state root's
`.locks` directory and are kept after a scope is purged.

After reclaiming, each pass removes images nothing needs any more (see
[Custom sandbox images](#custom-sandbox-images)): derived images this state
directory built that no container of any state uses and that no scope under
it wants on its current harness image, and untagged images labelled
`tth.image=base` or `tth.image=gateway`, the leftovers of rebuilding the
harness and gateway images, that no container uses. Derived images of other
state directories on the same daemon are left alone. Images inherit labels, so
an untagged image of your own built `FROM` a harness image counts as a
leftover too. A pass never forces a removal, skips an image a preparation is
using, deletes the lock files of derived images that are gone, and skips image
cleanup when Docker is unreachable. Images rebuilt before the `tth.image`
label existed are not recognized; remove those once with `docker image prune`.
BuildKit's build cache is not touched; reclaim it with `docker builder prune`.

## Toolchains and caches

Agents (and workspace setup scripts, below) work in bind-mounted projects with
plain `uv`, `node`, `npm`, `pnpm` and `yarn`. Other tools a project needs in
its image, such as system packages or compilers, come from its sandbox
policy's image instructions (see [Custom sandbox images](#custom-sandbox-images)),
not from workspace setup. The proxy injects these variables
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
there and does nothing when it is not. It is for workspace dependencies only
(installing into the mounted project); it runs as the unprivileged service
user and cannot add system packages, which belong in the policy's image
instructions. A Python backend with a Node frontend
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

## Custom sandbox images

A project's sandbox policy may carry image instructions: Dockerfile text
without `FROM` (field `image_dockerfile`, at most 16,384 characters). They are
the only way to customize a sandbox image. TTH applies them to the harness
image of every kind the policy's sandboxes run, for example:

```dockerfile
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ rustc cargo \
    && rm -rf /var/lib/apt/lists/*
```

Contract:

- Allowed instructions: `RUN`, `ENV`, `ARG`, `USER`, `WORKDIR`, `COPY`, `ADD`
  and `LABEL`. Saving a policy rejects `FROM`, every other instruction
  (`CMD`, `ENTRYPOINT`, `HEALTHCHECK`, `VOLUME`, `EXPOSE`, `SHELL`, ...), any
  option on `RUN` (`--mount`, `--network`, `--security`, `--device`, ...) or
  on `ENV`, `ARG`, `USER`, `WORKDIR` and `LABEL`, and `COPY` and `ADD` options
  other than `COPY --from`, `--chown`, `--chmod`, `--link`, `--parents`,
  `--exclude` and `ADD --chown`, `--chmod`, `--link`, `--checksum`,
  `--keep-git-dir`, `--exclude`, `--unpack`, written as plain `--name=value`.
  It also rejects control characters and text ending inside a line
  continuation or an open heredoc. Comments, line continuations (blank and
  comment lines inside one are skipped, and the pieces are joined without a
  space) and heredocs follow Docker's rules. Line endings are normalized and
  blank text means no instructions.
- TTH never builds the text as written. It builds a Dockerfile it writes from
  what it parsed: every instruction on one line, `RUN` as a JSON exec form in
  the harness image's shell (`RUN ["/bin/sh", "-c", "<command>"]`) with `<`
  escaped and its heredoc bodies folded into the command, and `COPY` and `ADD`
  without heredocs as JSON arrays. BuildKit therefore finds no option,
  instruction or heredoc the check did not. A `COPY` or `ADD` heredoc is kept
  as a heredoc, so that instruction takes only plain paths and options,
  without quotes, backslashes or `$` variables, and names its heredocs
  `<<NAME`, `<<'NAME'` or `<<"NAME"`.
- The harness and gateway images mount no BuildKit cache, so nothing a policy
  build leaves in one can reach them. Harness images built before that change
  read a `/root/.cache/uv` cache mount; after rebuilding them, drop it with
  `docker builder prune --filter type=exec.cachemount`.
- The build has no context: `COPY` and `ADD` work only with `--from=<image>`,
  heredocs or URLs, never host files. It runs as root with the host's normal
  network access, not the policy's egress rules, on the Docker daemon's own
  builder (named after the current Docker context) whichever buildx builder
  is selected, so `FROM` finds the local harness image.
- After the text TTH appends a trailer, read from the harness image, that
  restores its `WORKDIR`, `HOME`, `DJANGO_SETTINGS_MODULE` and `USER`, so start
  with `USER root` to install packages; the split still runs as `agent`. A
  harness image that leaves `USER` or `WORKDIR` empty gets Docker's defaults,
  `root` and `/`.
- Install to system paths such as `/usr`, `/usr/local` or `/opt`. `/home/agent`
  and `/data` are named volumes, and image content there reaches only a new,
  empty volume.
- The image is tagged `tth-<kind>-custom:<hash>`, where the hash covers the
  state directory, the harness image id and the text, and carries the
  `tth.image=derived`, `tth.derived`, `tth.state`, `tth.kind`, `tth.base-id`,
  `tth.dockerfile-sha256` and `tth.contract` labels. List them with
  `docker images --filter label=tth.derived=1`. Proxies with different state
  directories on one daemon build and remove separate images.
- It is built lazily, when a sandbox of that policy is prepared, after the
  harness image is ensured and before the scope's lock is taken, so reaping or
  closing that scope never waits for a build. The first session after the
  instructions change or the harness image is rebuilt waits for the build;
  requests meanwhile get the retryable `sandbox_preparing` error, up to the
  build timeout. A rebuilt harness image changes the tag, so an image built on
  the old one is never used; its container is recreated on the new image at
  the next preparation. A harness image rebuilt during a build is built on
  once more, then preparation fails with `custom_image_build_failed`.
- Each build is checked against the harness image: user, working directory,
  entrypoint, command, health check, ports, volumes, stop signal and shell must
  be unchanged; `HOME`, `VIRTUAL_ENV` and every variable starting with `UV_`,
  `PYTHON`, `DJANGO_`, `TALKTOHARNESSES_` or `TTH_` must keep the harness
  image's value, or stay unset; `PATH` must keep the harness image's
  directories in order (adding to it is fine); and the harness image's layers
  must come first. A failed build or check removes the image and fails
  preparation with `sandbox_unavailable`, reason `custom_image_build_failed`;
  the build output is only in the proxy log. There is no fallback to the
  harness image.
- The gateway never receives the instructions, and a policy without them is
  stored and served without the `image_dockerfile` field, so clients built
  before it existed still read it. Old images are removed by the scope reaper
  (see above).

Project dependencies still belong in `.tth/setup.sh`.

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
runs `tests/live/test_sandbox_docker.py` and
`tests/live/test_custom_sandbox_image_live.py`. The latter builds small images
on the local `tth-codex` image from instructions with a `COPY` heredoc, a
`RUN` heredoc script and `<<` in a command, checks that the derived image runs
as `agent` with their results, gets a new tag after its base is rebuilt, and
that image cleanup removes the superseded one. It removes everything it
created, and its cleanup runs under a temporary state directory, so it never
removes another proxy's images.

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
