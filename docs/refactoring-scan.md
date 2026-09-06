# Refactoring scan

State of the tree after the September 2026 pass. The first scan (pre-split)
found a streaming write tax in the Django projections plus copy-paste across
the in-tree provider packages. The service split moved the providers into the
`tth-<kind>` directories, which turned the copy-paste finding into a
*vendoring* finding: the split decision deliberately duplicates non-schema
shared code into every split, so the risk is silent drift rather than
duplication as such.

Do not restructure the layers. Persistence protocol, domain transitions, broker
composition, and the adapter/normalizer/schema triad are the right shapes.

## What changed in this pass

### Splits

| Finding | Fix |
| --- | --- |
| Vendored copies had drifted (supervisor `env` support, `Coroutine` typing, a stale multi-kind executable table, an `authenticate` allowlist entry, an empty `runtime/__init__`, a stderr fallback in model discovery) | Copies re-synced; `scripts/check_split_drift.py` diffs every vendored module after normalizing the per-split tokens and runs in the static CI gate |
| `tth-prime-agent` shipped a full ACP JSON-RPC package it never imported | Deleted; only `acp/framing.py` (JSONL) remains |
| `shared/sse_decoder.py` copied into splits that never used it | Deleted from grok, cursor, prime-agent |
| Conformance test carried the pre-split multi-kind `_launch` branches | Each split keeps only its own kind's launch values |

### Proxy

| Finding | Fix |
| --- | --- |
| `materialize_projections` rewrote shell columns and every interaction/activity row and rebuilt the search document on every commit | Shell write dropped (caller already stores it); rows come from `_apply_event` when events are present; search document rebuilds only when a text-bearing event is in the batch |
| One `get_worker_snapshot` SELECT per harness event | One per batch; flush already reloads, re-checks the binding, and rebases on conflict |
| Owner-scoped GET took `select_for_update` | Lock-free read with a version/sequence re-check and bounded retry |
| Replay serialized each event three times | Once in persistence for the byte cap, once in SSE for cap and frame |
| `_load` round-tripped rows through `json.dumps` | Lax pydantic validation of the stored JSON object |
| Two parallel batch-commit bodies with different error codes for a missing row | One `_commit_batch` with `_lock_aggregate_for_commit`; missing conversation is `INVALID_STATE` on every worker-scoped commit |
| Seventy-one hand-written `sync_to_async` wrappers | `@_db_thread` forwards the typed async signature to its `_name` twin |
| `command_projection` copied three times | `CommandProjection.from_command` |
| `remote/sandbox.py` mixed orchestration, Docker mechanics, and credential seeding | `remote/docker_ops.py` and `remote/sandbox_auth.py`; the manager keeps orchestration and thin delegates |

## Still open

Ordered by payoff. None of these change the layer structure.

| # | Finding | Where | Notes |
| --- | --- | --- | --- |
| 1 | `acp/normalizer.py` in grok and cursor differ by a cursor-only `usage` argument and a `cachedReadTokens` key | `tth-grok`, `tth-cursor` | Provider-specific today; if grok grows the same fields, sync them and add the file to the drift check |
| 2 | Four CLI probes still repeat the `--version` subprocess block | `tth-{grok,cursor,opencode,prime-agent}/harness/probe.py` | ~25 lines each; a vendored `shared/version_probe.py` would remove it |
| 3 | `sync_active_binding` runs two statements per commit | `django/materialize.py` | Cheap; skip unless the batch carries a binding-changing event |
| 4 | `_recompute_derived_title` SELECTs the earliest user message on every text-bearing commit | `django/materialize.py` | Could be keyed off the first user message id instead |
| 5 | Near-identical test copies across splits (`conftest.py`, `test_split_api.py`) | `tth-*/tests` | Formatting and version strings differ; add to the drift check once aligned |

## Largest modules (lines)

Size is not a refactor target; `transitions.py` and `runtime/manager.py`
should stay cohesive.

| Module | Lines |
| --- | ---: |
| `django/persistence.py` | ~3,300 |
| `runtime/manager.py` | 1,881 |
| `domain/transitions.py` | 1,640 |
| `application/command_processor.py` | 1,410 |
| `application/service.py` | 1,300 |
| `client.py` | 1,113 |
| `remote/sandbox.py` | ~680 |
