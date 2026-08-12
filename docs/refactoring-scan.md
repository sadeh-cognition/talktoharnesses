# Refactoring scan

Biggest-bang opportunities in talktoharnesses. The hexagonal split is sound —
the debt is a streaming write tax in Django projections, plus horizontal
copy-paste across the six provider packages.

Do not restructure the layers. Persistence protocol, domain transitions, broker
composition, and the adapter/normalizer/schema triad are the right shapes.
Split internals and delete duplication; do not invent a seventh abstraction
layer.

Snapshot of the tree at the time of this scan: 12 ranked findings, 4 high
impact, ~500 duplicated provider lines, largest file
`django/persistence.py` at 3,732 lines.

## Where to spend the next sprint

Recommended effort mix if the goal is user-visible speed plus maintainability.
Not a line-count breakdown.

| Share | Focus |
| --- | --- |
| 35% | Projection resync |
| 20% | Per-event snapshot reload |
| 20% | ACP adapter DRY |
| 15% | Commit boilerplate |
| 10% | Probes + compatibility |

## Largest modules (lines)

Source: `wc -l` on `src/talktoharnesses`. Size is not a refactor target —
`transitions.py` and `runtime/manager.py` should stay cohesive.

| Module | Lines |
| --- | ---: |
| `django/persistence.py` | 3732 |
| `runtime/manager.py` | 1906 |
| `domain/transitions.py` | 1596 |
| `application/service.py` | 1241 |
| `application/command_processor.py` | 1083 |
| `client.py` | 911 |
| `application/worker_coordinator.py` | 900 |
| `providers/cursor/adapter.py` | 849 |

## Ranked opportunities

Impact is user-visible speed or lines of duplicated knowledge, not file size.
Risk is regression surface on commit/OCC and adapter handshake paths.

| # | Finding | Lens | Impact | Risk | Payoff | Where |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Stop full projection resync on every commit | Performance | High | Med | O(history) → O(events) on the streaming path | `django/materialize.py` |
| 2 | Skip DB snapshot reload per harness event | Performance | High | Med | One SELECT per delta instead of per event | `command_processor._on_harness_event` |
| 3 | Extract shared ACP adapter for Grok + Cursor | DRY | High | Med | ~300–350 duplicated lines removed | `providers/grok` + `cursor/adapter.py` |
| 4 | Unify Django commit OCC boilerplate | DRY | High | High | 11 `_commit_*` methods share lock/version/seq checks | `django/persistence.py` |
| 5 | Shared CLI version-probe helper | DRY | Med | Low | ~150 lines across 4 near-identical probes | grok/cursor/opencode/prime_agent `probe.py` |
| 6 | Drop write-lock from GET snapshot | Performance | Med | Low | Reads stop blocking concurrent commits | `persistence._get_conversation_snapshot` |
| 7 | Serialize SSE replay events once | Performance | Med | Low | Replay gate is p95 ≤ 2s for 5,000 events | `django/api/sse.py` |
| 8 | One `enforce_published_operation` factory | DRY | Med | Low | Byte-identical function copied 6 times | `providers/*/compatibility.py` |
| 9 | Extract Cursor model/mode config from adapter | KISS | Med | Low | 849-line adapter → ~570; config is the bulk | `providers/cursor/adapter.py` |
| 10 | Avoid `json.dumps` round-trip on hot loads | Performance | Med | Low | Snapshot + replay skip encode/decode | `persistence._load`, `projections._validate_json` |
| 11 | Delete unused compatibility helpers and shims | YAGNI | Low | Low | Dead types, write-only fields, grok render shim | `compatibility.py`, adapters, `grok/render_supported.py` |
| 12 | One `command_projection` helper | DRY | Low | Low | Identical 8-line mapper exists in 3 places | `service.py`, `interaction_broker.py`, `projections.py` |

## 1. The streaming write tax

Every 50ms delta batch already writes shell columns in `_store_aggregate`.
`materialize_projections` then rewrites them and rebuilds search from the full
history.

| Step on each commit | Cost | Needed? |
| --- | --- | --- |
| `UPDATE ConversationAggregate` shell columns again | Duplicate write | No — `_store_aggregate` already did this |
| `update_or_create` every interaction | O(interactions) | No — `_apply_event` already upserts changes |
| `update_or_create` every activity | O(activities) | No — same as interactions |
| `_refresh_search_document`: load all messages + tools | O(history) SELECT + rewrite | Only when title/message/tool text changed |
| `_apply_event` for the committed batch | O(events) | Yes — this is the incremental path |
| `get_worker_snapshot` inside `_on_harness_event` | 1 SELECT per event, even with `batcher.state` | Only when binding/version must be rechecked |

### What to change

In `django/materialize.py`: drop the shell `UPDATE` (lines 76–87). Stop the
full interaction and activity loops (94–121). Rebuild the search document only
when the batch contains title, message, or tool-text events.

In `command_processor._on_harness_event`: when `batcher.state` is present,
reuse it for dispatch. Reload `get_worker_snapshot` only if the managed runtime
binding looks stale or a flush is about to commit.

In `_get_conversation_snapshot`: remove `select_for_update`. Owner-scoped GET
should not take a write lock. Keep the lock on the 11 commit methods.

## 2. Horizontal copy-paste in providers

ACP transport, ACP normalizer, and compatibility validation are already shared.
What remains is the same lifecycle copied into each provider package.

### Grok and Cursor adapters share ~300 lines

SequenceMatcher ratio ~0.64. Shared: connection wiring, initialize handshake,
start/resume/submit/interrupt, permission requests, prompt watcher, events
generator, close. Cursor-only bulk is ~280 lines of model/mode config —
extract that to `cursor/model_config.py` either way.

Extract `AcpHarnessAdapter` with hooks for protocol, identity validation, and
Cursor’s `_force` auto-approve. Do not fold Prime Agent into ACP — it is JSONL,
not JSON-RPC 2.0.

### Four CLI probes are the same function

grok, cursor, opencode, and prime_agent each: resolve executable, run
`--version`, check returncode, decode stdout, `match_release`,
`to_harness_capabilities`. One helper with harness label + match function.
Codex/Claude SDK probes stay separate.

### `enforce_published_operation` copied six times

Identical body except loader and `harness_label`. Same for
`to_harness_capabilities` and CompatibilitySection properties. A factory plus
a small section base class; keep `render_release_rows` per provider.

## 3. persistence.py is a monolith of repeated commits

The Persistence protocol (74 methods, testable via MemoryPersistence) should
stay. The Django implementation repeats lock → version check → sequence check
→ store → materialize across 11 `_commit_*` methods. Unify that kernel; do not
split the file into ten modules.

Do this after the projection fix. Collapsing commit helpers has a high
regression surface (OCC, fencing, recovery, harness switch). Fix the
materialize hot path first so the unified commit calls a cheap projector.

## Small DRY / YAGNI cleanup

| Item | Lens | Action |
| --- | --- | --- |
| `command_projection` in service, interaction_broker, projections | DRY | Keep `projections.command_projection`; delete the two copies |
| SSE `_byte_size` then `_event_frame` both call `model_dump_json` | Performance | Cache JSON bytes in `_bounded_replay` |
| `_load` / `_validate_json`: `model_validate_json(json.dumps(value))` | Performance | One helper; prefer `model_validate` if types already match |
| `EmptyExtraNotes`, `SharedMatrices` — defined, never imported | YAGNI | Delete |
| `_active_prompt_request_id` assigned in grok/cursor, never read | YAGNI | Delete the field |
| `release_by_id` on grok/cursor compatibility docs, never called | YAGNI | Delete |
| `grok/render_supported.py` 8-line shim to `providers.render_supported` | YAGNI | Point the one test at the real module |
| `event_dispatcher.apply_outcome_unknown` one-line wrapper | KISS | Call `domain.transitions.mark_outcome_unknown` directly |

## Well-designed — do not refactor

- `application/broker` wrapping into `django/broker` (SQLite vs PostgreSQL LISTEN)
- Persistence Protocol
- `domain/transitions.py` as a single state machine
- EventDispatcher as a thin harness→domain adapter
- DeltaBatcher 50ms window
- Handoff vs Transcript document types
- `django/api/schemas.py` (requests) vs domain projections (responses)
- `_sse.py` decoder vs `django/api/sse.py` encoder
- Provider argv files — launch shapes are genuinely different

Also skip: merging Claude+Codex into an SDK base (approval models differ),
forcing Prime Agent onto `AcpConnection`, splitting `service.py` into multiple
facades, plugin-style loader try/except for in-package providers.

## Suggested sequence

### Pass 1 — speed, contained risk

Projection incrementalism, per-event snapshot skip, drop GET
`select_for_update`, SSE serialize-once. These hit the documented replay and
SSE budgets without touching adapter contracts.

### Pass 2 — provider DRY

CLI probe helper, `enforce_published_operation` factory, then
`AcpHarnessAdapter` + Cursor `model_config` extract. Adapter tests already
exist per provider.

### Pass 3 — commit kernel, then dead code

Unify `_commit_*` only after materialize is cheap. Finish with unused types,
write-only fields, and the grok render shim.
