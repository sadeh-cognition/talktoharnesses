# Per-driver capabilities

| Capability | Claude | Codex | Cursor | Grok | OpenCode |
|---|---|---|---|---|---|
| Transport | Claude Agent SDK | `codex app-server` stdio JSON-RPC | `cursor-agent acp` | `grok agent stdio` | `opencode serve` HTTP+SSE |
| `interrupt_turn` | yes | yes | yes | yes | yes |
| Approvals | `can_use_tool` bridge | server requestApproval | ACP `request_permission` | ACP `request_permission` | `permission.asked` + HTTP reply |
| User input | no (v1) | yes | yes (elicitation) | yes (elicitation) | yes (questions) |
| Resume session | yes (`resume=`) | yes (`thread/resume`) | yes (`session/load`) | yes (`session/load`) | no (v1) |
| `model=` at session start | yes | yes (`thread/start`) | best-effort¹ | best-effort¹ | no |
| In-session model switch | yes | no | no | no | no |
| Mock peer tests | fake `ClaudeSDKClient` | `codex_mock_peer.py` | `acp_mock_agent.py` | `acp_mock_agent.py` | `opencode_mock_server.py` |

¹ ACP (as of `agent-client-protocol` 0.12.0) has no `session/set_model`; agents
expose model choice as a *select* config option. The driver looks for one and
applies it via `session/set_config_option`. If the agent offers no matching
option, it emits a `runtime.warning` with code `model_not_applied` and leaves
`Session.model` as `None` — it never reports a model that is not in effect.

## Approval decision mapping

| Canonical | Claude | Codex | ACP | OpenCode |
|---|---|---|---|---|
| `accept` | `PermissionResultAllow` | `accept` | `allow_once` | `once` |
| `accept_for_session` | `PermissionResultAllow` + local allowlist² | `acceptForSession` | `allow_always` | `always` |
| `decline` | `PermissionResultDeny` | `decline` | `reject_once` / cancelled | `reject` |

² The Claude Agent SDK's permission callback has no allow-always outcome, so
the driver records the tool name and auto-allows it for the rest of the session
rather than re-prompting.

## Event ordering notes

- **`request.resolved`** is emitted when the provider confirms the decision, not
  when the client sends it. For OpenCode that means the server's
  `permission.replied` event; a rejected reply raises from `respond()` and emits
  nothing.
- **`item.completed`** for a tool call waits for the tool result. A turn that ends
  with calls still open closes them with `status="incomplete"` so no
  `item.started` is left dangling.
- **`content.delta`** for Claude comes from partial `StreamEvent`s when they are
  available and from the assembled `AssistantMessage` otherwise — never both, so
  concatenating the stream yields the text exactly once.

## Install extras (uv)

```bash
uv sync --extra claude      # claude-agent-sdk
uv sync --extra acp         # agent-client-protocol (Cursor, Grok)
uv sync --extra opencode    # httpx + httpx-sse
uv sync --all-extras        # everything
# default `uv sync` already includes the dev group + all extras
```

Codex needs no extra Python packages (stdlib + transports); the `codex` CLI must be on `PATH` for live use.
