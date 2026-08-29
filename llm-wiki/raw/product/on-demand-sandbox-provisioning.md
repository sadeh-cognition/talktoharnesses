# On-Demand Sandbox Provisioning Requirements

Product input approved on 2026-08-30.

## Intent

TalkToHarnesses must not require operators to enable harness kinds through
environment variables. When any operation resolves a harness kind's endpoint,
the proxy spawns that kind's Docker split sandbox on demand. The proxy knows
which sandboxes it has spawned and keeps track of their endpoints itself.

## Provisioning

- Resolving a kind's endpoint spawns its Docker sandbox on demand. There is no
  per-kind enablement configuration (`TTH_SANDBOX_KINDS` is removed).
- Split URL overrides (`TTH_SPLIT_<KIND>_URL`) are removed. The proxy-managed
  Docker sandbox is the only split deployment path.
- A missing sandbox image is built locally by the proxy from the repository's
  per-kind build context. Installations without build contexts on disk (wheel
  installs) fail with an actionable error directing the operator to pre-build.
- Every spawned sandbox is recorded in the proxy database: kind, container
  name, image, host port, endpoint base URL, split token, and status. After a
  proxy restart, the persisted split token lets the proxy reattach to its
  running containers instead of destroying and recreating them.
- While an image build or container boot is still in progress, a request for
  that kind fails with a retryable `sandbox_preparing` error; unrecoverable
  sandbox failures surface as `sandbox_unavailable` with a fixed-vocabulary
  actionable message (Docker unreachable, build failed, missing build context,
  missing credential file, port conflict, health timeout).
- Background readiness probing never spawns containers or builds images; it
  probes only kinds whose sandbox is already running.

## Supersession

This source supersedes the operator-configuration and fail-closed statements
of `raw/product/split-service-runtime-ownership.md` — specifically that
operators configure a kind with `TTH_SPLIT_<KIND>_URL` or `TTH_SANDBOX_KINDS`,
that missing split configuration fails closed, and that operators choose
between an explicit split URL and a managed Docker split. That source remains
preserved as historical product input. Its split-ownership statements (each
split owns its provider adapter, discovery, authentication, compatibility
floor, and supervised process), the managed-container deployment boundary, and
the compatibility aggregation contract remain in force.

## Acceptance criteria

1. Any harness kind's first endpoint resolution spawns its Docker sandbox
   without prior configuration.
2. A missing sandbox image is built locally when build contexts are available;
   otherwise the failure names the pre-build remedy.
3. Spawned sandboxes are recorded in the proxy database, and a restarted proxy
   reattaches to running containers using the persisted split token.
4. Requests during preparation fail with a retryable `sandbox_preparing`
   error; sandbox failures surface actionable `sandbox_unavailable` messages.
5. Background readiness probing never creates containers or builds images.
