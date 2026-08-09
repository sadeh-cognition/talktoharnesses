# Supported Harnesses

This document is generated from packaged compatibility data.
Do not edit provider tables by hand; regenerate via
`python -m talktoharnesses.providers.render_supported`.

## Grok

- Adapter version: `2026.8.0.dev9`

### Known releases (implementation targets)

| Release ID | CLI version | Build | ACP | Platforms | Resume | Steer |
| --- | --- | --- | --- | --- | --- | --- |
| `grok-1.0.0-3cd0d0cbce` | 1.0.0 | `3cd0d0cbce` | v1 | linux, darwin, win32 | yes | no |

### Published create matrix

_No published create combinations yet. Create rows are added only after the opt-in create suite passes._

### Published resume matrix

_No published resume combinations yet. Resume rows are added only after the opt-in resume suite passes._

## Cursor

- Adapter version: `2026.8.0.dev9`

### Known releases (implementation targets)

| Release ID | CLI version | ACP | Platforms | Resume | Steer |
| --- | --- | --- | --- | --- | --- |
| `cursor-2026.08.04-aaa8809` | 2026.08.04-aaa8809 | v1 | linux, darwin, win32 | yes | no |

### Published create matrix

_No published create combinations yet. Create rows are added only after the opt-in create suite passes._

### Published resume matrix

_No published resume combinations yet. Resume rows are added only after the opt-in resume suite passes._

## Codex

- Adapter version: `2026.8.0.dev9`

### Known releases (implementation targets)

| Release ID | SDK | Runtime | Platforms | Resume | Steer |
| --- | --- | --- | --- | --- | --- |
| `codex-openai-codex-0.144.4` | 0.144.4 | openai-codex-cli-bin 0.144.4 | linux, darwin, win32 | yes | yes |

### Published create matrix

_No published create combinations yet. Create rows are added only after the opt-in create suite passes._

### Published resume matrix

_No published resume combinations yet. Resume rows are added only after the opt-in resume suite passes._

### Notes

- `codex-openai-codex-0.144.4`: Public AsyncCodex lacks a deferred approval handler; create/resume matrices stay empty until a release exposes broker-compatible approvals without private client attributes.

## Claude Code

- Adapter version: `2026.8.0.dev9`

### Known releases (implementation targets)

| Release ID | SDK | CLI | Source | Platforms | Resume | Steer |
| --- | --- | --- | --- | --- | --- | --- |
| `claude-agent-sdk-0.1.53-bundled-2.1.88` | 0.1.53 | 2.1.88 | bundled | linux, darwin, win32 | yes | no |

### Published create matrix

_No published create combinations yet. Create rows are added only after the opt-in create suite passes._

### Published resume matrix

_No published resume combinations yet. Resume rows are added only after the opt-in resume suite passes._

### Notes

- `claude-agent-sdk-0.1.53-bundled-2.1.88`: Implementation target using SDK-bundled Claude Code CLI; matrix rows published only after create/resume gates pass.

## OpenCode

- Adapter version: `2026.8.0.dev9`

### Known releases (implementation targets)

| Release ID | CLI version | Platforms | Resume | Steer |
| --- | --- | --- | --- | --- |
| `opencode-1.2.27` | 1.2.27 | linux, darwin, win32 | yes | no |

### Published create matrix

_No published create combinations yet. Create rows are added only after the opt-in create suite passes._

### Published resume matrix

_No published resume combinations yet. Resume rows are added only after the opt-in resume suite passes._

### Notes

- `opencode-1.2.27`: Implementation target for opencode serve loopback HTTP/SSE; matrix rows published only after create/resume gates pass.
