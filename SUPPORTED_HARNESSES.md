# Supported Harnesses

This document is generated from packaged compatibility data.
Do not edit provider tables by hand; regenerate via
`python -m talktoharnesses.providers.render_supported`.

## Grok

- Adapter version: `2026.8.5`

### Known releases (implementation targets)

| Release ID | CLI version | Build | ACP | Platforms | Resume | Interrupt | Steer | Multi-interaction | Nested |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `grok-1.0.0-3cd0d0cbce` | 1.0.0 | `3cd0d0cbce` | v1 | linux, darwin, win32 | yes | yes | no | yes | no |
| `grok-1.0.3-1a29d5bc12` | 1.0.3 | `1a29d5bc12` | v1 | linux | yes | yes | no | yes | no |
| `grok-1.0.4-d846eb93d9` | 1.0.4 | `d846eb93d9` | v1 | linux | yes | yes | no | yes | no |
| `grok-1.0.5-5115b46bc9` | 1.0.5 | `5115b46bc9` | v1 | linux | yes | yes | no | yes | no |

### Published create matrix

| Release ID | Platform |
| --- | --- |
| `grok-1.0.0-3cd0d0cbce` | `linux` |
| `grok-1.0.3-1a29d5bc12` | `linux` |
| `grok-1.0.4-d846eb93d9` | `linux` |
| `grok-1.0.5-5115b46bc9` | `linux` |

### Published resume matrix

| Release ID | Platform |
| --- | --- |
| `grok-1.0.0-3cd0d0cbce` | `linux` |
| `grok-1.0.3-1a29d5bc12` | `linux` |
| `grok-1.0.4-d846eb93d9` | `linux` |
| `grok-1.0.5-5115b46bc9` | `linux` |

### Published steer matrix

_No published steer combinations yet. Steer rows are added only after the opt-in steer gate passes._

### Published interrupt matrix

| Release ID | Platform |
| --- | --- |
| `grok-1.0.0-3cd0d0cbce` | `linux` |
| `grok-1.0.3-1a29d5bc12` | `linux` |
| `grok-1.0.4-d846eb93d9` | `linux` |
| `grok-1.0.5-5115b46bc9` | `linux` |

### Published multi-interaction matrix

| Release ID | Platform |
| --- | --- |
| `grok-1.0.0-3cd0d0cbce` | `linux` |
| `grok-1.0.3-1a29d5bc12` | `linux` |
| `grok-1.0.4-d846eb93d9` | `linux` |
| `grok-1.0.5-5115b46bc9` | `linux` |

### Published nested-activity matrix

_No published nested-activity combinations yet. Nested-activity rows are added only after the opt-in nested-activity gate passes._

## Cursor

- Adapter version: `2026.8.5`

### Known releases (implementation targets)

| Release ID | CLI version | ACP | Platforms | Resume | Interrupt | Steer | Multi-interaction | Nested |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `cursor-2026.08.04-aaa8809` | 2026.08.04-aaa8809 | v1 | linux, darwin, win32 | yes | yes | no | yes | no |
| `cursor-2026.08.11-e8db854` | 2026.08.11-e8db854 | v1 | linux | yes | yes | no | yes | no |

### Published create matrix

| Release ID | Platform |
| --- | --- |
| `cursor-2026.08.04-aaa8809` | `linux` |
| `cursor-2026.08.11-e8db854` | `linux` |

### Published resume matrix

| Release ID | Platform |
| --- | --- |
| `cursor-2026.08.04-aaa8809` | `linux` |
| `cursor-2026.08.11-e8db854` | `linux` |

### Published steer matrix

_No published steer combinations yet. Steer rows are added only after the opt-in steer gate passes._

### Published interrupt matrix

| Release ID | Platform |
| --- | --- |
| `cursor-2026.08.04-aaa8809` | `linux` |
| `cursor-2026.08.11-e8db854` | `linux` |

### Published multi-interaction matrix

| Release ID | Platform |
| --- | --- |
| `cursor-2026.08.04-aaa8809` | `linux` |
| `cursor-2026.08.11-e8db854` | `linux` |

### Published nested-activity matrix

_No published nested-activity combinations yet. Nested-activity rows are added only after the opt-in nested-activity gate passes._

## Codex

- Adapter version: `2026.8.5`

### Known releases (implementation targets)

| Release ID | SDK | Runtime | Platforms | Resume | Interrupt | Steer | Multi-interaction | Nested |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `codex-openai-codex-0.144.4` | 0.144.4 | openai-codex-cli-bin 0.144.4 | linux, darwin, win32 | yes | yes | yes | no | no |

### Published create matrix

| Release ID | Platform |
| --- | --- |
| `codex-openai-codex-0.144.4` | `linux` |

### Published resume matrix

| Release ID | Platform |
| --- | --- |
| `codex-openai-codex-0.144.4` | `linux` |

### Published steer matrix

| Release ID | Platform |
| --- | --- |
| `codex-openai-codex-0.144.4` | `linux` |

### Published interrupt matrix

| Release ID | Platform |
| --- | --- |
| `codex-openai-codex-0.144.4` | `linux` |

### Published multi-interaction matrix

_No published multi-interaction combinations yet. Multi-interaction rows are added only after the opt-in multi-interaction gate passes._

### Published nested-activity matrix

_No published nested-activity combinations yet. Nested-activity rows are added only after the opt-in nested-activity gate passes._

### Notes

- `codex-openai-codex-0.144.4`: Broker-compatible approvals use public CodexClient(approval_handler=...) with ApprovalsReviewer.user. Live create/resume/interaction proven on linux. Steer and interrupt are published for the same linux gate.

## Claude Code

- Adapter version: `2026.8.5`

### Known releases (implementation targets)

| Release ID | SDK | CLI | Source | Platforms | Resume | Interrupt | Steer | Multi-interaction | Nested |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `claude-agent-sdk-0.1.53-bundled-2.1.88` | 0.1.53 | 2.1.88 | bundled | linux, darwin, win32 | yes | yes | no | yes | no |

### Published create matrix

| Release ID | Platform |
| --- | --- |
| `claude-agent-sdk-0.1.53-bundled-2.1.88` | `linux` |

### Published resume matrix

| Release ID | Platform |
| --- | --- |
| `claude-agent-sdk-0.1.53-bundled-2.1.88` | `linux` |

### Published steer matrix

_No published steer combinations yet. Steer rows are added only after the opt-in steer gate passes._

### Published interrupt matrix

| Release ID | Platform |
| --- | --- |
| `claude-agent-sdk-0.1.53-bundled-2.1.88` | `linux` |

### Published multi-interaction matrix

| Release ID | Platform |
| --- | --- |
| `claude-agent-sdk-0.1.53-bundled-2.1.88` | `linux` |

### Published nested-activity matrix

_No published nested-activity combinations yet. Nested-activity rows are added only after the opt-in nested-activity gate passes._

### Notes

- `claude-agent-sdk-0.1.53-bundled-2.1.88`: Live create/resume/interaction proven on linux with SDK-bundled Claude Code CLI. Interrupt and multi-interaction are published for the same linux gate. Nested activity is not published: adapters do not emit activity_started.

## OpenCode

- Adapter version: `2026.8.5`

### Known releases (implementation targets)

| Release ID | CLI version | Platforms | Resume | Interrupt | Steer | Multi-interaction | Nested |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `opencode-1.2.27` | 1.2.27 | linux, darwin, win32 | yes | yes | no | yes | no |

### Published create matrix

| Release ID | Platform |
| --- | --- |
| `opencode-1.2.27` | `linux` |

### Published resume matrix

| Release ID | Platform |
| --- | --- |
| `opencode-1.2.27` | `linux` |

### Published steer matrix

_No published steer combinations yet. Steer rows are added only after the opt-in steer gate passes._

### Published interrupt matrix

| Release ID | Platform |
| --- | --- |
| `opencode-1.2.27` | `linux` |

### Published multi-interaction matrix

| Release ID | Platform |
| --- | --- |
| `opencode-1.2.27` | `linux` |

### Published nested-activity matrix

_No published nested-activity combinations yet. Nested-activity rows are added only after the opt-in nested-activity gate passes._

### Notes

- `opencode-1.2.27`: Live create/resume/interaction proven on linux for opencode serve loopback HTTP/SSE. Interrupt and multi-interaction are published for the same linux gate. Child sessions fold into the parent turn; nested activity is not published.

## Prime Agent

- Adapter version: `2026.8.5`

### Known releases (implementation targets)

| Release ID | CLI version | Transport | Platforms | Resume | Interrupt | Steer | Multi-interaction | Nested |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `prime-agent-0.7.1` | 0.7.1 | JSONL RPC | linux, darwin | yes | yes | yes | no | no |

### Published create matrix

| Release ID | Platform |
| --- | --- |
| `prime-agent-0.7.1` | `linux` |

### Published resume matrix

| Release ID | Platform |
| --- | --- |
| `prime-agent-0.7.1` | `linux` |

### Published steer matrix

| Release ID | Platform |
| --- | --- |
| `prime-agent-0.7.1` | `linux` |

### Published interrupt matrix

| Release ID | Platform |
| --- | --- |
| `prime-agent-0.7.1` | `linux` |

### Published multi-interaction matrix

_No published multi-interaction combinations yet. Multi-interaction rows are added only after the opt-in multi-interaction gate passes._

### Published nested-activity matrix

_No published nested-activity combinations yet. Nested-activity rows are added only after the opt-in nested-activity gate passes._

### Notes

- `prime-agent-0.7.1`: Uses Prime Agent's official JSONL RPC mode. Create, resume, steer, and interrupt are published for the linux live gate.
