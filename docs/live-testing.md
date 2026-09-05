# Live harness testing

Opt-in live gates prove create, resume, and advertised-capability support
against the packaged compatibility floor through the official HTTP client and
an in-process Django ASGI proxy worker. They use disposable workspaces, may
incur provider cost, and must never run against an untrusted repository change
with production credentials.

The proxy worker always uses `RemoteHarnessAdapter` and spawns each kind's
Docker sandbox on demand — no split configuration is needed beyond Docker and
the kind's credential file. When a live flag is enabled, missing credentials,
SDKs, executables, a CLI below the floor, or missing advertised capabilities
are **failures**, not skips.

The manual GitHub workflow targets provider-specific self-hosted runner labels:
`self-hosted`, `talktoharnesses-live`, and the provider name. Each runner needs
Docker and the provider's credential file in the runner user's `HOME`. The job
pre-builds the kind's image with `deploy/build-splits.sh` so the pytest run
does not spend its timeout on the build. It does not install native harnesses
or synthesize credential files.

## Flags and selectors

Every provider's live gate runs the full journey through a TTH-managed Docker
container. Enable it with `TALKTOHARNESSES_LIVE_<KIND>_SANDBOX=1` and run
`tests/live/test_<kind>_sandbox_live.py`:

| Provider | Flag | Pytest selector |
| --- | --- | --- |
| Grok | `TALKTOHARNESSES_LIVE_GROK_SANDBOX=1` | `tests/live/test_grok_sandbox_live.py` |
| Cursor | `TALKTOHARNESSES_LIVE_CURSOR_SANDBOX=1` | `tests/live/test_cursor_sandbox_live.py` |
| Codex | `TALKTOHARNESSES_LIVE_CODEX_SANDBOX=1` | `tests/live/test_codex_sandbox_live.py` |
| Claude Code | `TALKTOHARNESSES_LIVE_CLAUDE_SANDBOX=1` | `tests/live/test_claude_sandbox_live.py` |
| OpenCode | `TALKTOHARNESSES_LIVE_OPENCODE_SANDBOX=1` | `tests/live/test_opencode_sandbox_live.py` |
| Prime Agent | `TALKTOHARNESSES_LIVE_PRIME_AGENT_SANDBOX=1` | `tests/live/test_prime_agent_sandbox_live.py` |
| Muse Code | `TALKTOHARNESSES_LIVE_MUSE_SANDBOX=1` | `tests/live/test_muse_sandbox_live.py` |

The fixture boots an isolated container from the `tth-<kind>:latest` image,
seeds the kind's host credential file (override the source with
`TTH_SANDBOX_<KIND>_AUTH_FILE`), strips the kind's API-key env var so seeded
host-file authentication is what's proven, and removes the container and its
volumes afterward. A missing image is built on demand; pre-build with
`deploy/build-splits.sh` to keep the gate fast. See
[`deploy/README.md`](../deploy/README.md) for the per-kind credential file
table.

The credential-free split integration gates
(`tests/live/test_split_claude_sandbox_echo.py`,
`tests/live/test_split_opencode_sandbox_echo.py`, enabled with
`TALKTOHARNESSES_SPLIT_INTEGRATION=1`) run the proxy journey against a
sandboxed split booted with the echo adapter, and double as the end-to-end
proof of on-demand sandbox provisioning including the local image build.

Run each gate as an isolated pytest invocation of that file. Do not mix live
files into a `pytest tests/` unit session: `tests/live/conftest.py` switches the
session Django database to file-backed SQLite so the in-process worker and ASGI
requests share one connection.

Example:

```bash
uv sync --locked --extra django --extra client
deploy/build-splits.sh latest cursor   # optional pre-build
TALKTOHARNESSES_LIVE_CURSOR_SANDBOX=1 \
uv run pytest tests/live/test_cursor_sandbox_live.py -q
```

A conventional executable override such as `TALKTOHARNESSES_CURSOR_EXECUTABLE`
belongs in the split image's environment, not the proxy test process.

## What each gate proves

Each gate drives `AsyncTalkToHarnessesClient` against the production worker
composition (Django persistence, remote adapter registry, runtime manager) over
ASGI. Provider adapters and harness processes live in the configured split.

1. Create a harness and probe it. Assert `supports_resume`. The probed identity
   must meet the packaged floor; it need not match a specific patch. Print the
   probed version and `version_advisory` status.
2. Create a conversation and submit a unique deterministic prompt.
3. Consume canonical SSE `ConversationEvent`s through the authoritative
   terminal event. Answer `interaction_requested` events with
   `resolve_interaction`.
4. For the successful create and resume turns, observe `usage_updated` before
   the terminal event. Every reported token value must be a nonnegative integer,
   and at least one reported value must be positive. Providers may omit token
   categories they do not report.
5. Close the proxy-managed runtime (native session id is retained), submit a
   second unique prompt, observe `session_resumed` with that same id, and
   assert the first turn is not replayed.
6. Exercise broker-compatible approval/question handling when the live stream
   surfaces interactions for advertised capabilities.
7. For each advertised capability, run the matching feature gate on the resumed
   conversation:
   - **multi-interaction** — one turn that defers at least two interactions
   - **nested activity** — observe `activity_started` (unpublished until a
     normalizer emits it)
   - **steer** — steer an in-flight turn and reach `turn_completed`
   - **interrupt** — interrupt an in-flight turn and reach `turn_interrupted`
8. Shut the worker down so no owned task, client, responder, process, or
   descendant remains.

Live tests may print probed versions, advisory status, and pass/fail state. They
must not print prompts, credentials, environment values, native payloads, or
executable paths beyond the configured path already known to the operator.

## After a live gate

A passing live gate on a platform already covered by the floor does **not**
require a JSON edit for the CLI to be accepted at runtime. Optionally bump
`latest_verified` in that harness's packaged compatibility document and
regenerate `SUPPORTED_HARNESSES.md` with
`uv run python scripts/render_supported.py` so the advisory tracks the last
live proof.

Raise the floor only when the adapter can no longer drive older identities.
Add a platform to `floor.platforms` only after a live gate passes on that
platform. Adapter-owned capability flags change only when the adapter itself
gains or loses an operation.
