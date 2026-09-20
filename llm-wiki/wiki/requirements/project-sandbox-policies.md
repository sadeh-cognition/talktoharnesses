---
type: requirement
title: Project sandbox policies
status: implemented
audiences: [product, developer]
tags: [type/requirement, status/implemented]
last_verified: 2026-09-20
verified_against_commit: bb531a658b1e9ddbe510b3fe07ab4e7170b03fdb
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

Each immutable policy revision, provider, and mount set gets separate home/data
volumes and an internal Docker network. A gateway joins that network and a public
network. It has no forwarding capability. Only the public interception certificate
enters the sandbox. Split control uses a separate host token. Existing sessions
resume with their original policy revision. Secret-bearing MCP headers/URLs are
rejected. Direct DSPy execution is outside this boundary.

The command blocklist applies to Claude Bash pre-execution callbacks and command
approval requests from other providers. It cannot prevent arbitrary processes
launched through unobserved tools. Coverage is exposed with the conversation.


## Gap

The installed versions of all seven providers pass live create and resume
checks through the gateway, including verification of the requested reply.
Native credential formats and endpoints can change; rerun this gate after provider
upgrades. Refresh rotation, cross-scope JWT handles, and secret filtering are
covered by deterministic gateway tests; forced live refresh was not exercised
for every provider.
Existing deployment images must be rebuilt with the shared policy wire types and
the gateway image. Legacy sessions without a policy cannot enter managed sandboxes.
Command guards are best effort; scripts and unobserved execution paths can bypass
them. Network and credential boundaries remain independent of command approvals.

## Acceptance criteria

- Deny network access except admitted HTTPS requests and the exact split control route.
- Keep usable provider secrets and the gateway CA private key outside agent containers.
- Preserve session policy revisions across resume and reject stale settings writes.
- Publish only the assigned branch and recorded commit to the saved repository.
- Report command interception coverage accurately and enforce denials before approvals.

## Implementation evidence

`tth-types/src/tth_types/sandbox.py`, `src/talktoharnesses/gateway/`,
`src/talktoharnesses/remote/scoped_sandboxes.py`,
`src/talktoharnesses/remote/isolated_sandbox.py`, and
`src/talktoharnesses/django/sandbox_policies.py`.
The recorded commit is the inspected baseline; this page describes the associated
uncommitted worktree changes.

## Test evidence

`tests/unit/gateway/`, `tests/unit/django/test_sandbox_store.py`,
and `tests/unit/remote/test_sandbox_and_registry.py`. Docker checks additionally
exercised real package reads, denied direct network access, and command checks. The six real Docker gates cover boundary enforcement,
workspace setup and split integration. `tests/live/test_policy_provider_sessions.py`
passes create/resume checks for Grok, Cursor, Codex, Claude, OpenCode, Prime Agent
and Muse. Grok resume now advances its synthetic frame offset past persisted
offsets before replay, preventing fresh reply chunks from being discarded.
The proxy suite passes 845 tests with 91.20% coverage; the changed Claude
and Grok split suites pass 106 and 154 tests. Lint, migrations, split drift,
Dockerfile rendering and wiki checks pass.

## Related

- [Project sandbox policy request](../../raw/product/project-sandbox-policies.md)
