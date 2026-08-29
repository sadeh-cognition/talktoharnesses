# Deploying talktoharnesses (tth-proxy) with split services

The `talktoharnesses` package is now **tth-proxy**: it owns the client-facing
API, persistence, auth, and orchestration. Its adapter path is generic; sandbox
lifecycle seeds each kind's host credential file into the managed home volume
when the container is created. Each harness kind runs as its own split service
from a top-level project directory:

| Kind | Directory | Image | Default port |
|---|---|---|---|
| grok | `tth-grok` | `tth-grok` | 8111 |
| cursor | `tth-cursor` | `tth-cursor` | 8112 |
| codex | `tth-codex` | `tth-codex` | 8113 |
| claude | `tth-claude` | `tth-claude` | 8114 |
| opencode | `tth-opencode` | `tth-opencode` | 8115 |
| prime_agent | `tth-prime-agent` | `tth-prime-agent` | 8116 |

All six expose the identical HTTP+SSE API defined by the shared
[`tth-types`](../tth-types) package (`tth_types.split_api`).

## Building the split images

```sh
deploy/build-splits.sh            # all six, tag "latest"
deploy/build-splits.sh v1 claude  # one kind, custom tag
```

Images embed the harness CLI (chowned to the build UID/GID so the split's
executable-ownership check passes) and run
`uvicorn tth_<kind>.asgi:application` on container port 8010.

Pre-building is an optimization, not a requirement: the proxy builds a missing
`tth-<kind>` image itself on the first request for that kind (editable/repo
installs only — a wheel install has no build contexts on disk and fails with
`sandbox_unavailable` until the image is pre-built).

## Running the proxy

The proxy's `SandboxManager` spawns each kind's container on demand the first
time its endpoint is resolved, reuses it afterwards, and records it in the
`talktoharnesses_sandbox` table together with the generated split token, so a
restarted proxy reattaches to running containers instead of recreating them.
While an image build or container boot is still in progress, requests for that
kind fail with `sandbox_preparing` — retry shortly. No per-kind enablement
configuration exists.

The configured mount roots (`TTH_SANDBOX_MOUNT_ROOTS`, default `$HOME/dev`)
are bind-mounted into each container **at the same path**; harness configs and
git worktrees embed absolute paths, so working directories and workspace roots
must live under one of them. Paths outside them are rejected with
`sandbox_path_not_mounted` (HTTP 400) before the split is contacted. Changing
the roots recreates each kind's container on its next request.

Tuning environment (all optional):

- `TTH_SANDBOX_MOUNT_ROOTS` — colon-separated absolute host paths bind-mounted
  into every sandbox (default: `$HOME/dev`). Keep this as narrow as your
  projects allow; an empty value mounts nothing.
- `TTH_SANDBOX_IMAGE_TAG`, `TTH_SPLIT_PORT_<KIND>` — image tag / host port.
- `TTH_SANDBOX_ENV_<KIND>` — comma-separated env vars forwarded into that
  kind's container (defaults: `XAI_API_KEY`, `CURSOR_API_KEY`,
  `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENCODE_API_KEY` for their kinds;
  prime_agent forwards none).
- `TTH_SANDBOX_<KIND>_AUTH_FILE` — optional credential file source, seeded
  into the managed `tth-<kind>-home` volume when that kind's container is
  created. By default TTH uses the kind's conventional host file when present;
  the forwarded env var remains the fallback when no file is available:

  | Kind | Host default | Container target |
  |---|---|---|
  | grok | `$HOME/.grok/auth.json` | `/home/agent/.grok/auth.json` |
  | cursor | `$HOME/.config/cursor/auth.json` | `/home/agent/.config/cursor/auth.json` |
  | codex | `$HOME/.codex/auth.json` | `/home/agent/.codex/auth.json` |
  | claude | `$HOME/.claude/.credentials.json` | `/home/agent/.claude/.credentials.json` |
  | opencode | `$HOME/.local/share/opencode/auth.json` | `/home/agent/.local/share/opencode/auth.json` |
  | prime_agent | `$HOME/.prime/config.json` | `/home/agent/.prime/config.json` |

The split token sent as `X-TTH-Split-Token` is generated per sandbox and
persisted (in the clear) in the proxy database; it guards loopback-only
traffic between the proxy and its containers, which share a trust domain.

Containers are created with `restart: unless-stopped`, `cap_drop: ALL`,
`no-new-privileges`, `pids_limit 512`, `mem_limit 4g`, a per-kind
`tth-<kind>-home` volume for CLI credentials, and are **left running** when
the proxy stops so later requests reuse them.

Interactive CLI logins persist in the per-kind home volume, e.g.:

```sh
docker exec -it tth-claude claude login
```

## OpenTelemetry

The proxy and all six splits export traces, metrics, and logs by default via
OTLP/HTTP:

- `OTEL_EXPORTER_OTLP_ENDPOINT=false` (or `0`, case-insensitive) disables all
  signals; any other value is the collector endpoint; unset uses the SDK
  default `http://localhost:4318`. Missing SDK/exporter packages with export
  enabled fail startup — opt out or install them.
- Each split bakes its own `service.name` (`tth-grok` … `tth-prime-agent`,
  overridable per process via `OTEL_SERVICE_NAME`); the proxy reports
  `talktoharnesses`.
- The proxy always injects the endpoint into sandbox containers (independent
  of `TTH_SANDBOX_ENV_<KIND>`), rewriting unset/localhost values to
  `http://host.docker.internal:4318` and adding the
  `host.docker.internal:host-gateway` extra-hosts mapping so containers reach
  a collector on the host. The `false`/`0` sentinel passes through verbatim so
  opted-out proxies get opted-out splits. Collector headers can contain
  credentials, so `OTEL_EXPORTER_OTLP_HEADERS` is forwarded only when
  `TTH_SANDBOX_FORWARD_OTEL_HEADERS=1`; `OTEL_SERVICE_NAME` is never forwarded.
- Upgrading to this behavior recreates each kind's container once (env/
  extra-hosts drift; the home/data volumes survive). Rebuild the split images
  first — containers running old images ignore the telemetry env.

## Live gates

The docker sandbox path has an opt-in gate: `TALKTOHARNESSES_SANDBOX_DOCKER=1`
runs `tests/live/test_sandbox_docker.py`.

Every kind has a sandbox live gate: `tests/live/test_<kind>_sandbox_live.py`,
enabled with `TALKTOHARNESSES_LIVE_<KIND>_SANDBOX=1`. The fixture selects the
sandbox kind, assigns an isolated container and port, and cleans them up
afterward. The full create/resume journey uses the official TTH HTTP client
while TTH handles workspace mounting, localhost routing, split-token setup,
managed container startup, and credential seeding:

```sh
TALKTOHARNESSES_LIVE_GROK_SANDBOX=1 \
  uv run pytest tests/live/test_grok_sandbox_live.py -q -s --tb=short
```

Each sandbox gate specifically requires the kind's host credential file (see
the auth-file table above) and removes the kind's API-key env var, proving
that seeded host-file authentication works on its own. A missing
`tth-<kind>:latest` image is built on demand; pre-build it to keep the gate
fast. OpenCode's
host file is created by `opencode auth login`. Credential files are copied
only when a managed container is created, so recreate the `tth-<kind>`
container after changing the host auth file (the live fixtures sidestep this
by using a fresh container per run).
