---
type: requirement
title: Reclaim sandbox scopes
status: implemented
audiences:
  - product
  - developer
tags:
  - type/requirement
  - status/implemented
last_verified: 2026-09-26
verified_against_commit: 37c8daf9e2d18fc70439c98f2222174b51f29463
---

# Reclaim sandbox scopes

## Intent

Stop policy sandbox scopes from piling up on the Docker host, without losing
the ability to return to a finished conversation. Each immutable policy
revision, provider and mount set is its own scope, and every linked worktree
is part of the mount set, so a client that runs each job in its own worktree
(Agentbahn workflow runs) creates new scopes for every job. Before this change
nothing ever removed a scope. After a Docker Desktop restart, old scopes stayed
behind as unstartable containers. The operator asked on 2026-09-25 for
containers to be cleaned up automatically while finished runs stay resumable.

## Current behavior

A reaper started with the ASGI lifespan reclaims scopes in two tiers.

- **Containers are disposable.** The split container, the gateway and the
  internal network are removed once the scope has not been used for the
  container idle period (default one day). They are also removed at the first
  pass after either container stopped running, which is how Docker Desktop
  restarts leave them. The next session in the scope recreates all three. The
  named volumes are reattached, so harness sessions resume natively.
- **Session state is kept longer.** The `-home` and `-data` volumes, the
  private state directory and the sandbox row are purged in two cases: when a
  host path the scope mounts no longer exists (its worktree was deleted), or
  after the purge idle period (default 90 days, `0` keeps them).

Only scopes whose state directory is under the proxy's sandbox state root are
considered. The directory proves ownership and holds the evidence of use, so
scopes prepared under another state root (a second proxy, or a live test's
temporary root) are never touched even though they share the Docker daemon.
Directory names must match `tth-scope-` plus 24 hex characters and must not be
symlinks, so legacy `tth-<kind>` resources are out of reach. Each pass lists
the labelled containers and networks once and looks up an owned scope's
resources by the names its layout derives.

A scope counts as in use while a remote adapter is bound to it or its
preparation is running. Adapters acquire the scope when they bind to it, with
no await between resolving the scope and acquiring it, and hold it strongly
until they close. In-use scopes are skipped. Every pass touches their
`last-used` state file, including when reclaiming is disabled, and the pass
interval must be shorter than the container idle period. The scope's last use
is the newer of its `last-used` file and its state directory, which every
preparation updates.

One layout object in the sandbox layer names a scope's containers, network,
volumes, state files and lock, and performs its teardown. Preparation and
teardown share a cross-process lock kept in the state root's `.locks`
directory, outside the state directory a purge removes. Reclaiming is atomic
against adapter binding. The scoped sandbox manager refuses a scope that is in
use. Otherwise it removes the resources and, for a purge, the sandbox row
before callers resolving the scope stop waiting. Those callers then get a
fresh instance. Purging stops the scope's MCP relay before its socket and
state are deleted.

A failed removal keeps the row, so the next pass retries it. A pass that
cannot reach Docker is skipped. Gateway preparation reuses only a running
gateway and recreates a stopped one, since the gateway keeps its state in the
bind-mounted state directory and a Docker Desktop restart can leave a stopped
gateway unstartable.

Clients can also stop a scope on demand.
`POST /conversations/{id}/runtime/close?release_sandbox=true` closes the
conversation's runtime as usual, then resolves the scope from the
conversation's binding and stops it: the containers and network go, while the
volumes, state directory and row stay. Because the scope comes from the
binding, the stop also works when the idle reap already closed the runtime. A
scope that another runtime in this process uses is kept. A failed stop is
only logged; the reaper stops the scope later.

Settings: `TTH_SANDBOX_REAPER=0` disables reclaiming;
`TTH_SANDBOX_REAP_INTERVAL_SECONDS` (default 600),
`TTH_SANDBOX_CONTAINER_IDLE_SECONDS` (default 86400) and
`TTH_SANDBOX_PURGE_IDLE_SECONDS` (default 7776000). The test suite disables
reclaiming by default so a started lifespan never touches the developer's
sandboxes.

## Gap

In-use tracking is per process. With several proxy processes sharing a state
root, the reaper judges a scope used only by another process by its
`last-used` file, but an on-demand stop cannot see that use and removes the
other process's containers under its live runtime. Docker resources of scopes
whose state directory is gone are left for the operator, and so are sandbox
rows without a state directory. Lock files are never removed. Scopes that no
client stops still rely on the idle periods and on deleted worktrees. The
reaper has not been exercised by a live Docker gate.

## Acceptance criteria

- Dead and idle scope containers, gateways and networks are removed without
  deleting session volumes or state.
- Returning to a reclaimed scope recreates its containers and resumes the
  native session.
- Session volumes, state and rows are purged when a mounted path is gone or
  after the configurable purge period, which can be disabled.
- Scopes in use and resources outside the scope naming scheme are never
  removed.

## Implementation evidence

- `src/talktoharnesses/remote/scope_layout.py`: `ScopeLayout` (names, lock,
  `touch`, `remove`), `SCOPE_NAME` and `SCOPE_LABEL`
- `src/talktoharnesses/remote/scope_reaper.py`: policy, fact gathering, the
  pure `decide` and the loop
- `src/talktoharnesses/remote/scoped_sandboxes.py`: `owned_scopes`,
  `touch_in_use`, the atomic `reclaim`, the on-demand `stop` and the Docker
  client factory the reaper shares
- `src/talktoharnesses/application/service.py`: `close_runtime` stopping the
  binding's scope; `src/talktoharnesses/django/api/routes.py` and
  `src/talktoharnesses/client.py`: the `release_sandbox` query parameter
- `src/talktoharnesses/remote/isolated_sandbox.py`: `acquire`, `release`,
  `in_use`, layout-based naming and the gateway start fallback
- `src/talktoharnesses/remote/adapter.py`: the adapter's scope lease
- `src/talktoharnesses/remote/mcp_relay.py`: `stop_mcp_relay`
- `src/talktoharnesses/remote/sandbox.py` and
  `src/talktoharnesses/django/sandbox_store.py`: `SandboxStore.delete`
- `src/talktoharnesses/django/asgi.py`: lifespan wiring
- `deploy/README.md`: operator documentation

The recorded commit is the inspected baseline; this page describes the
associated uncommitted worktree changes.

## Test evidence

`tests/unit/remote/test_scope_reaper.py` covers:

- dead and idle stops that keep volumes and state
- purges on a missing worktree and after the purge period, and purges
  disabled with `0`
- scopes in use, released scopes, and use recorded while reclaiming is off
- on-demand stops that keep volumes, state and row, and refuse scopes in use
- scopes owned by another state root, symlinks and foreign names
- retrying a failed purge, and passes when Docker is unreachable
- resolution waiting until a reclaim has deleted the row
- reclaiming waiting for a preparation's lock, and the lock surviving a purge
- environment parsing and the interval check

The existing tests also cover:

- `tests/unit/remote/test_isolated_sandbox.py`: recreating a stopped gateway
- `tests/live/test_sandbox_docker.py`: with real Docker, a stopped gateway is
  replaced by a new running container attached to the scope network with its
  alias, and the agent container is kept (verified on `ef27349`)
- `tests/unit/remote/test_remote_adapter.py`: acquire and release, and MCP
  relay URLs through a bound scope
- `tests/unit/remote/test_mcp_relay.py`: stopping a relay
- `tests/unit/django/test_sandbox_store.py`: row deletion
- `tests/unit/django/test_asgi.py`: lifespan wiring and the policy settings
- `tests/unit/application/test_service.py`: stopping the scope on close, after
  the runtime is already gone, and surviving a failed stop
- `tests/unit/django/test_api.py` and `tests/unit/test_client.py`: the
  `release_sandbox` query parameter

The proxy suite passes 936 tests outside `tests/live` with 91.86% coverage,
and lint passes.

## Related

- [Project sandbox policies](project-sandbox-policies.md)
- [Provision sandbox workspaces](provision-sandbox-workspaces.md)
- [Requirements by Status](../maps/requirements-by-status.md)
