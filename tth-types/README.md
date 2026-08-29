# tth-types

Shared wire schemas for the talktoharnesses proxy (`tth-proxy`) and the per-harness
split services (`tth-grok`, `tth-cursor`, `tth-codex`, `tth-claude`, `tth-opencode`,
`tth-prime-agent`).

Schemas only: frozen pydantic v2 models, wire-stable enums, and the `DomainError`
error contract. No runtime logic lives here — process supervision, adapters, and
protocol machinery belong to the split services; orchestration and persistence
belong to the proxy.

Modules:

- `tth_types.base` — shared pydantic config (`FROZEN`), UTC datetime handling, ID aliases
- `tth_types.enums` — wire-stable enumerations (`HarnessKind`, `ErrorCode`, statuses)
- `tth_types.errors` — `DomainError` with stable codes and public messages
- `tth_types.harness` — harness configuration, capabilities, launch snapshot, interaction payloads
- `tth_types.events` — canonical conversation event envelope and payload union
- `tth_types.adapter` — the `HarnessAdapter` protocol and its request/session models
- `tth_types.process` — process-local lifecycle events
- `tth_types.split_api` — HTTP request/response bodies and SSE frames of the split service API
