# Live harness testing

Opt-in live gates prove exact create/resume support claims. They use disposable
workspaces, may incur provider cost, and must never run against an untrusted
repository change with production credentials.

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

Example:

```bash
uv sync --locked --extra django --extra cursor
TALKTOHARNESSES_LIVE_CURSOR=1 \
TALKTOHARNESSES_CURSOR_EXECUTABLE=/path/to/cursor \
uv run pytest tests/live/test_cursor_live.py -q
```

## What each gate proves

1. Probe and assert the exact release identity represented by a proposed matrix row.
2. Start a fresh session through public adapter/runtime contracts.
3. Submit a unique deterministic prompt, consume normalized events through the
   authoritative terminal event, and close the first local adapter/runtime.
4. Construct a new adapter (and supervised process when process-bound), resume the
   retained native session ID, import persisted native dedupe identity, submit a
   second unique prompt, and assert the first turn is not replayed.
5. Exercise broker-compatible approval/question handling when the live stream
   surfaces interactions for advertised capabilities.
6. Interrupt/close remaining activity with no owned resources left.

Live tests may print fixed release IDs and pass/fail state. They must not print
prompts, credentials, environment values, native payloads, or executable paths
beyond the configured path already known to the operator.

## Publishing matrix rows

After a live gate passes on a platform, add only that exact
`{release_id, platform}` create/resume row to the packaged JSON document and
regenerate `SUPPORTED_HARNESSES.md`. Do not list untested platforms.
