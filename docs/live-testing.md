# Live harness testing

Opt-in live gates prove exact create, resume, and advertised-capability support
claims through the official HTTP client against an in-process Django ASGI
worker. They use disposable workspaces, may incur provider cost, and must never
run against an untrusted repository change with production credentials.

When a live flag is enabled, missing credentials, SDKs, executables, exact
versions, or capabilities are **failures**, not skips.

The manual GitHub workflow targets provider-specific self-hosted runner labels:
`self-hosted`, `talktoharnesses-live`, and the provider name. Each protected
runner must already have that provider's pinned executable and authentication;
the workflow does not install native harnesses or synthesize credential files.

## Flags and selectors

| Provider | Flag | Extra setup | Pytest selector |
| --- | --- | --- | --- |
| Grok | `TALKTOHARNESSES_LIVE_GROK=1` | `TALKTOHARNESSES_GROK_EXECUTABLE` | `tests/live/test_grok_live.py` |
| Cursor | `TALKTOHARNESSES_LIVE_CURSOR=1` | `TALKTOHARNESSES_CURSOR_EXECUTABLE` | `tests/live/test_cursor_live.py` |
| Codex | `TALKTOHARNESSES_LIVE_CODEX=1` | `codex` extra + provider auth in the OS environment | `tests/live/test_codex_live.py` |
| Claude Code | `TALKTOHARNESSES_LIVE_CLAUDE=1` | `claude` extra; optional `TALKTOHARNESSES_CLAUDE_EXECUTABLE` | `tests/live/test_claude_live.py` |
| OpenCode | `TALKTOHARNESSES_LIVE_OPENCODE=1` | `opencode` extra + `TALKTOHARNESSES_OPENCODE_EXECUTABLE` | `tests/live/test_opencode_live.py` |
| Prime Agent | `TALKTOHARNESSES_LIVE_PRIME_AGENT=1` | `TALKTOHARNESSES_PRIME_AGENT_EXECUTABLE` | `tests/live/test_prime_agent_live.py` |

Run each gate as an isolated pytest invocation of that file. Do not mix live
files into a `pytest tests/` unit session: `tests/live/conftest.py` switches the
session Django database to file-backed SQLite so the in-process worker and ASGI
requests share one connection.

Example:

```bash
uv sync --locked --extra django --extra client --extra cursor
TALKTOHARNESSES_LIVE_CURSOR=1 \
TALKTOHARNESSES_CURSOR_EXECUTABLE=/path/to/cursor \
uv run pytest tests/live/test_cursor_live.py -q
```

## What each gate proves

Each gate drives `AsyncTalkToHarnessesClient` against the production worker
composition (Django persistence, default adapter registry, runtime manager)
over ASGI. It does not construct provider adapters or spawn processes in the
test.

1. Create a harness and probe it. Assert `supports_resume` and match
   `capabilities.version` to the exact packaged release identity represented by
   a proposed matrix row.
2. Create a conversation and submit a unique deterministic prompt.
3. Consume canonical SSE `ConversationEvent`s through the authoritative
   terminal event. Answer `interaction_requested` events with
   `resolve_interaction`.
4. Close the in-process runtime (native session id is retained), submit a
   second unique prompt, observe `session_resumed` with that same id, and
   assert the first turn is not replayed.
5. Exercise broker-compatible approval/question handling when the live stream
   surfaces interactions for advertised capabilities.
6. For each advertised capability, run the matching feature gate on the resumed
   conversation:
   - **multi-interaction** — one turn that defers at least two interactions
   - **nested activity** — observe `activity_started` (unpublished until a
     normalizer emits it)
   - **steer** — steer an in-flight turn and reach `turn_completed`
   - **interrupt** — interrupt an in-flight turn and reach `turn_interrupted`
7. Shut the worker down so no owned task, client, responder, process, or
   descendant remains.

Live tests may print fixed release IDs and pass/fail state. They must not print
prompts, credentials, environment values, native payloads, or executable paths
beyond the configured path already known to the operator.

## Publishing matrix rows

After a live gate passes on a platform, add only that exact
`{release_id, platform}` row to the matching packaged matrix (`create_matrix`,
`resume_matrix`, `steer_matrix`, `interrupt_matrix`,
`multi_interaction_matrix`, or `nested_activity_matrix`) and regenerate
`SUPPORTED_HARNESSES.md`. Do not list untested platforms. A capability flag on
the release record is an implementation target; only a matrix row is a published
support claim. Stable validation rejects an advertised capability with no matrix
row.
