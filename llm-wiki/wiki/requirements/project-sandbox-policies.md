---
type: requirement
title: Project sandbox policies
status: implemented
audiences:
  - product
  - developer
tags:
  - type/requirement
  - status/implemented
last_verified: 2026-09-22
verified_against_commit: bd5ffc2c6887ee9ef6354d4d8d84254acd1d5be4
---

# Project sandbox policies

## Intent

Limit harness network access, keep provider credentials outside the agent's
filesystem and environment, constrain host publication, and reject blocked tool
commands. See the [approved request](../../raw/product/project-sandbox-policies.md).

## Current behavior

The editable project policy contains an exact HTTPS host/path allowlist,
provider selection, read-only dependency roots, and additional command prefixes.
Defaults allow provider operation and Python/npm registry reads. The gateway
rejects private DNS results, direct tunnels, unapproved redirects, and Git receive-pack.
Native authentication files and environment keys are replaced with scoped handles.
Only admitted authentication fields receive real credentials. Token refresh is
serialized against the host file and responses return handles.
Cursor API-key login is a separate exchange operation. Its access and refresh
tokens are stored in the gateway's private state with mode 0600, survive gateway
restart, and reach the agent only as handles. Initial login does not require a
pre-existing host login file and does not overwrite an existing login.

Each immutable policy revision, provider, and mount set gets separate home/data
volumes and an internal Docker network. A gateway joins that network and a public
network. It has no forwarding capability. Only the public interception certificate
enters the sandbox. Split control uses a separate host token. Existing sessions
resume with their original policy revision. MCP servers reach the agent only
as opaque gateway URLs without headers. The gateway strips agent-supplied
credentials and forwards those URLs over a Unix socket in its private state
directory to a relay in the proxy process. The relay restores the stored URL
and headers, so header secrets never enter the agent container and loopback
servers on the proxy host stay reachable. Routes persist in gateway state and
the relay restarts with foreground preparation after a proxy restart.
Gateway reconciliation compares the container's recorded image id, so a
rebuilt gateway image replaces a gateway whose original image was deleted. MCP URLs
carrying userinfo or a query string are rejected. Direct DSPy execution is
outside this boundary.
Background readiness checks resolve the full policy, provider, and mount identity
and consult persisted sandbox records after a proxy restart. Probes receive
only an existing endpoint and cannot prepare, repair, or build a sandbox. Both
the agent and its gateway must be running, and the stored endpoint must be
ready. An unhealthy or disappearing gateway makes the probe fail without
entering foreground preparation. Gateway preparation
reconciles the private-network attachment even on existing containers. A failed
gateway replacement leaves its previous configuration recorded so retries
cannot mistake it for a completed replacement. The private gateway configuration
records the resolved host credential file as well as its container path.
Changing credential directories, including files with the same basename,
replaces the gateway and preserves the agent container.
Private host state records the daemon's effective bind sources and container ID
at creation. Reuse verifies these exact sources, including Docker Desktop's
translated VM paths, alongside mount permissions and the configured destinations.

The command blocklist applies to Claude Bash pre-execution callbacks and command
approval requests from other providers. It cannot prevent arbitrary processes
launched through unobserved tools. Coverage is exposed with the conversation.


## Gap

The initial policy implementation passed live create and resume checks for all
seven installed providers, including verification of the requested reply.
Native credential formats and endpoints can change; rerun this gate after provider
upgrades. Refresh rotation, cross-scope JWT handles, and secret filtering are
covered by deterministic gateway tests; forced live refresh was not exercised
for every provider.
Existing deployment images must be rebuilt with the shared policy wire types and
the gateway image. Legacy sessions without a policy cannot enter managed sandboxes.
Command guards are best effort; scripts and unobserved execution paths can bypass
them. The MCP relay is covered by unit tests and a real Unix socket crossing into
a Docker Desktop container; no live provider gate exercises an MCP tool call
through it yet, and the relay buffers each request body. Network and credential boundaries remain independent of command approvals.

## Acceptance criteria

- Deny network access except admitted HTTPS requests and the exact split control route.
- Keep usable provider secrets and the gateway CA private key outside agent containers.
- Preserve session policy revisions across resume and reject stale settings writes.
- Publish only the assigned branch and recorded commit to the saved repository.
- Report command interception coverage accurately and enforce denials before approvals.

## Implementation evidence

`tth-types/src/tth_types/sandbox.py`, `src/talktoharnesses/gateway/`,
`src/talktoharnesses/remote/scoped_sandboxes.py`,
`src/talktoharnesses/remote/isolated_sandbox.py`,
`src/talktoharnesses/remote/mcp_relay.py`, and
`src/talktoharnesses/django/sandbox_policies.py`.
The recorded commit is the inspected baseline; this page describes the associated
uncommitted worktree changes.

## Test evidence

`tests/unit/gateway/`, `tests/unit/remote/test_mcp_relay.py`,
`tests/unit/django/test_sandbox_store.py`,
and `tests/unit/remote/test_sandbox_and_registry.py`. Docker checks additionally
exercised real package reads, denied direct network access, and command checks. The six real Docker gates cover boundary enforcement,
workspace setup and split integration. `tests/live/test_policy_provider_sessions.py`
passes create/resume checks for Grok, Cursor, Codex, Claude, OpenCode, Prime Agent
and Muse. Grok resume now advances its synthetic frame offset past persisted
offsets before replay, preventing fresh reply chunks from being discarded.
The proxy suite passes 853 tests with 91.28% coverage; the changed Claude
and Grok split suites pass 106 and 154 tests. Lint, migrations, split drift,
Dockerfile rendering and wiki checks pass.
Review regression tests cover Cursor API-key exchanges with and without an
existing login, token handle reuse after gateway restart and rotation, readiness
across provider/revision/mount identities, and failed network attachment and
gateway replacement. The relevant tests are `tests/unit/gateway/test_gateway_http.py`,
`tests/unit/remote/test_scoped_sandboxes.py`, and
`tests/unit/remote/test_isolated_sandbox.py`.
`tests/live/test_sandbox_docker.py` passes with the rebuilt gateway image,
repairs a deliberately detached gateway network, switches credential directory
through a gateway replacement, preserves the agent container,
and verifies permitted package reads, denied direct egress, and command denial.
`tests/unit/remote/test_readiness_sandbox.py` exercises the production probe
factory after a restart with a changed image tag. Healthy, unhealthy and
disappearing gateways never enter preparation, and probe clients are closed.
The review pass used synthetic Cursor exchange credentials; it did not repeat
the provider inference gates or the split test suites.

## Related

- [Project sandbox policy request](../../raw/product/project-sandbox-policies.md)
