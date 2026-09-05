# tth-muse

Muse Code split service for TalkToHarnesses, using `muse serve` and the official Muse Session Protocol (MSP v1).

The proxy selects this service with `kind: "muse"`, starts its Docker sandbox
on demand, and uses the same authenticated HTTP/SSE contract as other harnesses.
The default host port is 8117; the container listens on 8010.

## Setup

Run `muse login` on the host, or set `META_API_KEY` for the proxy. TTH seeds
`~/.config/muse/auth.json` into the container when present. Override that source
with `TTH_SANDBOX_MUSE_AUTH_FILE`. Provider credentials stay in the split runtime.

Build from the repository root:

```bash
deploy/build-splits.sh latest muse
```

The image installs Muse with Meta's official installer and disables automatic
updates during execution. Probe requires Linux and a release at or above
`1.0.3-R2198.1`, including numeric R-build comparison, plus an MSP v1 handshake.
Rebuild the image to update the bundled CLI.

For local split development, install Muse on PATH or set
`TALKTOHARNESSES_MUSE_EXECUTABLE`, then run `make serve` in this directory.
`GET /v1/health` is unauthenticated; set `TTH_SPLIT_TOKEN` for the other routes.

## Behavior

- Persistent create/resume sessions, streamed assistant text and reasoning,
  tool events, per-turn model overrides, steering, and interruption.
- Approval choices retain Muse's requirement identity so one approval stage
  cannot answer another. Structured questions support single and multiple
  selections and free text.
- Approval handling follows Meta's SDK: `approval/request` receives a receipt
  acknowledgment, `approval/requested` opens the interaction, and the offered
  choice is sent through `approval/decide`. Commands use increasing UUIDv7 IDs,
  verify acknowledgment IDs, and retain their identity across at most three
  attempts for explicit overload/backpressure errors. Internal errors are surfaced.
  Concurrent or repeated answers share the first decision's outcome.
- Models come from MSP `model/list`. MSP exposes no mode or effort discovery;
  this adapter returns empty lists and rejects those configuration fields.
- `yolo: true` selects Muse's `allowAll` approval mode and disables its inner
  sandbox. The surrounding TTH Docker sandbox still applies.
- Token updates accumulate only the active turn's model calls. Provider-supplied
  `promptTokens` and `totalTokens` preserve Muse's cache accounting. Missing
  counts stay absent; terminal-only usage is emitted before the terminal event.
- Nested activity is not advertised. TTH retains its canonical transcript when
  resuming and does not replay Muse's historical messages into a new turn.

The HTTP/SSE wrapper, session store, and process supervisor follow the existing
self-contained split-service implementation. `tth-types` is the shared wire contract.

## Current live-validation limitation

Live testing of `1.0.3-R2198.1` passes create/resume with token usage. Steering
and interruption also pass a direct CLI check, including queued work following
a steered turn. However, Muse can reject `approval/decide` with MSP error
`-32603`: `approval ledger durability fence` with unflushed records. This blocks
reliable approval delivery. The live gate checks persisted answer-command
outcomes as well as interaction counts; a turn can finish despite a failed
approval command. The adapter surfaces the error and `latest_verified` remains unset.

Standalone checks on 2026-09-05 used Meta's TypeScript SDK with the same
`1.0.3-R2198.1` host, durable sessions, and `onRequest` approvals. Fresh sessions
accepted four decisions outside Docker and four inside Docker without ledger
errors. Resuming a previously completed session outside both TTH and Docker,
then issuing another tool approval, reproduced the exact `-32603` error with
`pending=13`. The SDK also received `approval/resolved` despite the failed
durability acknowledgment. The internal cause remains unconfirmed; adopting
the SDK's command handling does not resolve this native failure after resume.
Some approved tool calls separately failed with
`escalated execution requires an unrestricted permission profile`.

## Validation

```bash
uv run pytest
make lint
# From the repository root, with Docker and Muse credentials:
TALKTOHARNESSES_LIVE_MUSE_SANDBOX=1 uv run pytest tests/live/test_muse_sandbox_live.py
```

Protocol fixtures come from Meta's [official SDK source](https://github.com/meta-models/muse-code-sdk).
The Python implementation follows its [approval router](https://github.com/meta-models/muse-code-sdk/blob/main/clients/sdk-ts/src/facade/approval.ts)
and [command connection](https://github.com/meta-models/muse-code-sdk/blob/main/clients/sdk-ts/src/connection/connection.ts).
See the [MSP and SDK documentation](https://meta-models.github.io/muse-code-sdk/)
and [Muse Code overview](https://dev.meta.ai/docs/overview/) for the upstream contract.
