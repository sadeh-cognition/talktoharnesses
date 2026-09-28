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
last_verified: 2026-09-28
verified_against_commit: ef27349524da4db36ed49f48f6d2a9a49a32a876
---

# Project sandbox policies

## Intent

Limit harness network access, keep provider credentials outside the agent's
filesystem and environment, constrain host publication, and reject blocked tool
commands. See the [approved request](../../raw/product/project-sandbox-policies.md).

## Current behavior

The editable project policy contains an exact HTTPS host/path allowlist,
provider selection, read-only dependency roots, additional command prefixes,
and optional image instructions (Dockerfile text without `FROM`) that
[customize the sandbox images](customize-sandbox-images.md) of that policy.
Defaults allow provider operation and Python/npm registry reads and add no
image instructions. The gateway configuration omits the image instructions,
so the gateway never receives them. A policy without image instructions is
stored and served without the field, so readers built before it existed
still accept the policy. The gateway
rejects private DNS results, direct tunnels, unapproved redirects, and Git receive-pack.
Inside an admitted tunnel, every Host header and any request-target authority
(absolute-form or HTTP/2 `:authority`) must name the admitted host. Only ASCII
case, a trailing dot and port 443 may differ. Anything else is denied as
`egress_denied` before credentials are substituted, so a request cannot reach
another tenant of a CDN that routes by Host (domain fronting). The gateway's own
routes (split control, MCP relay, command check, Muse rewrite) set their own
Host header. The denial log names the tunnel's host, never the Host value the
agent sent.
Its event loop refuses a host name when any DNS answer is not a public address,
and mitmproxy connects only to the answers that were checked. The gateway
refuses every upstream host when it runs on another event loop, admits only
TCP upstream connections, and requires the split address to be an IP literal,
the only form that skips resolution. An upstream connection keeps the host
name as its address, so later requests on the same keep-alive tunnel reuse it
instead of opening another connection. Every denial is logged as
`sandbox_policy_denied` with the policy, revision, reason (`egress_denied`,
`port_denied`, `private_address` and others) and host; paths, queries, bodies
and headers are never logged.
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
The live Docker gate detaches a running gateway with `docker network disconnect`
and, in a separate step, stops one with `docker stop`. It does not reproduce the
invalid bind-mount sources that a Docker Desktop restart can leave behind.

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
`tests/live/test_sandbox_docker.py` detaches the running gateway from the
scope network and checks that preparation reattaches that same container with
its `tth-gateway.invalid` alias. It then stops the gateway and checks that
preparation replaces it with a new running container attached with that alias,
instead of restarting it. It switches credential directory
through a gateway replacement, preserves the agent container,
and verifies permitted package reads, denied direct egress, and command denial.
From `37c8daf`, which recreates stopped gateways, until 2026-09-28 the test
stopped the gateway before detaching it and then reloaded the removed
container, so it failed with Docker `NotFound` on every gateway image. On
`ef27349` it passes with a gateway image built from that commit and a Codex
image whose Python sources match it. With the reattachment removed from
preparation it fails its readiness wait, and with the alias dropped it fails
the alias assertion.
`tests/unit/remote/test_readiness_sandbox.py` exercises the production probe
factory after a restart with a changed image tag. Healthy, unhealthy and
disappearing gateways never enter preparation, and probe clients are closed.
The review pass used synthetic Cursor exchange credentials; it did not repeat
the provider inference gates or the split test suites.
Domain-fronting tests in `tests/unit/gateway/test_gateway_http.py` build flows
the way mitmproxy's transparent layer does inside a tunnel. They cover a
mismatched Host header, absolute-form target and HTTP/2 `:authority`, duplicate
Host headers, a non-default port, and a provider route carrying a credential
handle. They also check that case, trailing-dot and `:443` spellings are still
admitted and that denial logs omit the path, query and Host value.
`tests/unit/gateway/test_gateway_http.py` runs the real mitmproxy with the
gateway addon on the production event loop against a local TLS registry:
twelve requests through one keep-alive tunnel complete over a single upstream
handshake whose SNI is the host. It also covers resolution with a private
answer, `serve` choosing that loop, refusal on any other loop, the admitted
connection's reusable address, the split address check, and the logged
reason and host for each kind of denial. The live test sends eight requests
through one curl tunnel. Before its reattachment fix it ran from a scratch
copy, where that check passed on the fixed gateway image and timed out from
the sixth request on the previous one. On `8bb9734` with this fix, the whole
live test passes. In an isolated scope, `npm ci` of a
620-package frontend through the fixed gateway finished in 17 seconds; before
the fix it stalled after 75 tarballs. The proxy suite passes 976 tests (21 skipped).

## Related

- [Project sandbox policy request](../../raw/product/project-sandbox-policies.md)
- [Approved sandbox image customization](../../raw/product/sandbox-image-customization-amendment.md)
- [Customize sandbox images](customize-sandbox-images.md): image instructions in the policy and derived images.
- [Reclaim sandbox scopes](reclaim-sandbox-scopes.md): idle and dead scope containers are removed; session volumes stay for resume.
