# talktoharnesses

Unified **async** Python interface for coding-agent harnesses. One `Harness` protocol and one canonical `RuntimeEvent` union — a Python port of [T3 Code](https://github.com/pingdotgg/t3code)'s provider layer.

```python
from talktoharnesses import harness

async with harness("codex", cwd=".") as h:
    await h.start_session()
    async for ev in h.send_turn("fix the failing tests"):
        if ev.type == "content.delta":
            print(ev.text, end="")
        elif ev.type == "request.opened":
            await h.respond(ev.request_id, "accept")
```

## Five harnesses

| Harness  | Transport                                      | Extra          |
|----------|-----------------------------------------------|----------------|
| Claude   | `claude-agent-sdk` (`ClaudeSDKClient`)        | `[claude]`     |
| Codex    | `codex app-server` stdio JSON-RPC             | (none)         |
| Cursor   | `cursor-agent acp` (ACP JSON-RPC)             | `[acp]`        |
| Grok     | `grok agent stdio` (ACP JSON-RPC)             | `[acp]`        |
| OpenCode | `opencode serve` HTTP + SSE                   | `[opencode]`   |

See [docs/drivers/README.md](docs/drivers/README.md) for capability details.

## Install (uv)

This project is managed with [uv](https://docs.astral.sh/uv/).

```bash
# clone, then:
uv sync                          # creates .venv; package + dev group (all drivers)
```

Library-only install (no dev tools):

```bash
uv sync --no-dev                 # runtime deps only (pydantic)
uv sync --no-dev --extra claude  # + Claude SDK
uv sync --no-dev --all-extras    # + Claude, ACP, OpenCode
```

Requires Python 3.11+ (uv selects a compatible interpreter).

## Demo CLI

```bash
uv run python -m talktoharnesses --harness codex --cwd . "list the files here"
uv run talktoharnesses --harness claude --accept-all "explain this repo"
```

## Development

```bash
uv sync                  # install / refresh lock + env
uv run pytest            # primary gate — mock peers only, no agent CLIs required
uv run pytest -m live    # opt-in smoke against real CLIs (skip if binary missing)
uv run mypy --strict src/
uv run ruff check
```

Add a dependency:

```bash
uv add httpx             # runtime
uv add --group dev ruff  # dev group
uv add --optional claude claude-agent-sdk
uv lock                  # refresh lockfile after manual pyproject edits
```

### Codex schema regeneration

```bash
uv run python scripts/generate_codex_models.py --ref <openai/codex-sha>
```

Vendored schemas live under `src/talktoharnesses/codex/_generated/schemas/`.

## Design notes

- **Async-first** — no sync facade in v1.
- **Mock-first tests** — every driver has a fixture peer; CI never needs agent binaries.
- **Canonical events** keep T3's type strings (`content.delta`, `request.opened`, …) so the TypeScript adapters remain a usable reference.
- Deferred event families (`thread.realtime.*`, `task.*`, `hook.*`, …) surface as `runtime.warning` with `raw` attached.
