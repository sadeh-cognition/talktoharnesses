# Supported Harnesses

This document is generated from packaged compatibility data.
Do not edit provider tables by hand; regenerate via
`python -m talktoharnesses.providers.render_supported`.

## Grok

- Adapter version: `2026.8.2`

### Known releases (implementation targets)

| Release ID | CLI version | Build | ACP | Platforms | Resume | Steer |
| --- | --- | --- | --- | --- | --- | --- |
| `grok-1.0.0-3cd0d0cbce` | 1.0.0 | `3cd0d0cbce` | v1 | linux, darwin, win32 | yes | no |

### Published create matrix

| Release ID | Platform |
| --- | --- |
| `grok-1.0.0-3cd0d0cbce` | `linux` |

### Published resume matrix

| Release ID | Platform |
| --- | --- |
| `grok-1.0.0-3cd0d0cbce` | `linux` |

## Cursor

- Adapter version: `2026.8.2`

### Known releases (implementation targets)

| Release ID | CLI version | ACP | Platforms | Resume | Steer |
| --- | --- | --- | --- | --- | --- |
| `cursor-2026.08.04-aaa8809` | 2026.08.04-aaa8809 | v1 | linux, darwin, win32 | yes | no |

### Published create matrix

| Release ID | Platform |
| --- | --- |
| `cursor-2026.08.04-aaa8809` | `linux` |

### Published resume matrix

| Release ID | Platform |
| --- | --- |
| `cursor-2026.08.04-aaa8809` | `linux` |

## Codex

- Adapter version: `2026.8.2`

### Known releases (implementation targets)

| Release ID | SDK | Runtime | Platforms | Resume | Steer |
| --- | --- | --- | --- | --- | --- |
| `codex-openai-codex-0.144.4` | 0.144.4 | openai-codex-cli-bin 0.144.4 | linux, darwin, win32 | yes | yes |

### Published create matrix

| Release ID | Platform |
| --- | --- |
| `codex-openai-codex-0.144.4` | `linux` |

### Published resume matrix

| Release ID | Platform |
| --- | --- |
| `codex-openai-codex-0.144.4` | `linux` |

### Notes

- `codex-openai-codex-0.144.4`: Broker-compatible approvals use public CodexClient(approval_handler=...) with ApprovalsReviewer.user. Live create/resume/interaction proven on linux.

## Claude Code

- Adapter version: `2026.8.2`

### Known releases (implementation targets)

| Release ID | SDK | CLI | Source | Platforms | Resume | Steer |
| --- | --- | --- | --- | --- | --- | --- |
| `claude-agent-sdk-0.1.53-bundled-2.1.88` | 0.1.53 | 2.1.88 | bundled | linux, darwin, win32 | yes | no |

### Published create matrix

| Release ID | Platform |
| --- | --- |
| `claude-agent-sdk-0.1.53-bundled-2.1.88` | `linux` |

### Published resume matrix

| Release ID | Platform |
| --- | --- |
| `claude-agent-sdk-0.1.53-bundled-2.1.88` | `linux` |

### Notes

- `claude-agent-sdk-0.1.53-bundled-2.1.88`: Live create/resume/interaction proven on linux with SDK-bundled Claude Code CLI.

## OpenCode

- Adapter version: `2026.8.2`

### Known releases (implementation targets)

| Release ID | CLI version | Platforms | Resume | Steer |
| --- | --- | --- | --- | --- |
| `opencode-1.2.27` | 1.2.27 | linux, darwin, win32 | yes | no |

### Published create matrix

| Release ID | Platform |
| --- | --- |
| `opencode-1.2.27` | `linux` |

### Published resume matrix

| Release ID | Platform |
| --- | --- |
| `opencode-1.2.27` | `linux` |

### Notes

- `opencode-1.2.27`: Live create/resume/interaction proven on linux for opencode serve loopback HTTP/SSE.

## Prime Agent

- Adapter version: `2026.8.2`

### Known releases (implementation targets)

| Release ID | CLI version | Transport | Platforms | Resume | Steer |
| --- | --- | --- | --- | --- | --- |
| `prime-agent-0.7.1` | 0.7.1 | JSONL RPC | linux, darwin | yes | yes |

### Published create matrix

| Release ID | Platform |
| --- | --- |
| `prime-agent-0.7.1` | `linux` |

### Published resume matrix

| Release ID | Platform |
| --- | --- |
| `prime-agent-0.7.1` | `linux` |

### Notes

- `prime-agent-0.7.1`: Uses Prime Agent's official JSONL RPC mode; create, resume, streaming, steer, and interrupt are covered by adapter contract fixtures.
