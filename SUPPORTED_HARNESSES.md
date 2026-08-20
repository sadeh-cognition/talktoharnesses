# Supported Harnesses

This document is generated from packaged compatibility data.
Do not edit provider tables by hand; regenerate via
`python -m talktoharnesses.providers.render_supported`.

Each harness publishes a **floor** (minimum identity and platforms) and
adapter-owned capability flags. Models, modes, and efforts are discovered
at probe from the installed CLI. Newer identities above the floor are
accepted; `latest_verified` is advisory only.

## Grok

- Adapter version: `2026.8.5`
- Floor: CLI `>= 1.0.0` on linux
- Latest verified: `1.0.5 (5115b46bc9)` on linux
- Models, modes, and efforts are discovered at probe from the installed CLI.
- ACP: v1

### Adapter capabilities

| Resume | Interrupt | Steer | Multi-interaction | Nested |
| --- | --- | --- | --- | --- |
| yes | yes | no | yes | no |

### Notes

- Live create/resume/interaction proven on linux. Interrupt and multi-interaction are published for the same linux gate.

## Cursor

- Adapter version: `2026.8.5`
- Floor: CLI `>= 2026.08.04` on linux
- Latest verified: `2026.08.11-e8db854` on linux
- Models, modes, and efforts are discovered at probe from the installed CLI.
- ACP: v1

### Adapter capabilities

| Resume | Interrupt | Steer | Multi-interaction | Nested |
| --- | --- | --- | --- | --- |
| yes | yes | no | yes | no |

### Notes

- Live create/resume, permissions, model-family selection, parameter selection, and Agent/Plan/Ask selection proven on linux. Interrupt and multi-interaction are published for the same linux gate.

## Codex

- Adapter version: `2026.8.5`
- Floor: SDK `0.144.4` + `openai-codex-cli-bin` `0.144.4` (exact) on linux
- Latest verified: `codex-openai-codex-0.144.4` on linux
- Models, modes, and efforts are discovered at probe from the installed CLI.

### Adapter capabilities

| Resume | Interrupt | Steer | Multi-interaction | Nested |
| --- | --- | --- | --- | --- |
| yes | yes | yes | no | no |

### Notes

- Broker-compatible approvals use public CodexClient(approval_handler=...) with ApprovalsReviewer.user. Live create/resume/interaction proven on linux. Steer and interrupt are published for the same linux gate. SDK and runtime remain the extra pin.

## Claude Code

- Adapter version: `2026.8.5`
- Floor: SDK `0.1.53` + CLI `>= 2.1.88` on linux
- Latest verified: `claude-agent-sdk-0.1.53-bundled-2.1.88` on linux
- Models, modes, and efforts are discovered at probe from the installed CLI.

### Adapter capabilities

| Resume | Interrupt | Steer | Multi-interaction | Nested |
| --- | --- | --- | --- | --- |
| yes | yes | no | yes | no |

### Notes

- Live create/resume/interaction proven on linux with SDK-bundled Claude Code CLI. Interrupt and multi-interaction are published for the same linux gate. Nested activity is not published: adapters do not emit activity_started. SDK identity remains the extra pin; explicit CLI paths at or above the floor are accepted.

## OpenCode

- Adapter version: `2026.8.5`
- Floor: CLI `>= 1.2.27` on linux
- Latest verified: `opencode-1.2.27` on linux
- Models, modes, and efforts are discovered at probe from the installed CLI.

### Adapter capabilities

| Resume | Interrupt | Steer | Multi-interaction | Nested |
| --- | --- | --- | --- | --- |
| yes | yes | no | yes | no |

### Notes

- Live create/resume/interaction proven on linux for opencode serve loopback HTTP/SSE. Interrupt and multi-interaction are published for the same linux gate. Child sessions fold into the parent turn; nested activity is not published.

## Prime Agent

- Adapter version: `2026.8.5`
- Floor: CLI `>= 0.7.1` on linux
- Latest verified: `prime-agent-0.7.1` on linux
- Models, modes, and efforts are discovered at probe from the installed CLI.
- Transport: JSONL RPC

### Adapter capabilities

| Resume | Interrupt | Steer | Multi-interaction | Nested |
| --- | --- | --- | --- | --- |
| yes | yes | yes | no | no |

### Notes

- Uses Prime Agent's official JSONL RPC mode. Create, resume, steer, and interrupt are published for the linux live gate.
