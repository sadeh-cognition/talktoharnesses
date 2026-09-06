# Harness MCP servers requirements

Approved: 2026-09-06

A harness configuration must be able to name streamable HTTP MCP servers that
the harness attaches to every new and resumed session, so an integrating
application (Agentbahn's per-project memory bank is the first consumer) can
give a harness tools without touching provider config files.

- `HarnessConfiguration.mcp_servers` is a list of servers, each with a short
  identifier `name`, an absolute `http(s)` `url`, and optional request
  `headers`. Server names are unique within one configuration.
- Only HTTP transports are accepted. Splits run in sandboxes, so a command on
  the proxy host would not be reachable from the harness process.
- The field is fixed at harness creation and applies to new and resumed
  sessions alike, like `yolo`.
- Each split publishes `supports_mcp_servers` in its capability flags and in
  `SUPPORTED_HARNESSES.md`. Claude Code, Cursor, Grok, and Codex support the
  field. OpenCode, Muse Code, and Prime Agent do not; when their configuration
  names any server, probe, start, and resume fail with `provider_incompatible`
  and an actionable message rather than ignoring the servers.
- Supporting splits pass the servers in their provider's native shape: the
  Claude Agent SDK `mcp_servers` option, ACP `mcpServers` on `session/new`
  and `session/load`, and Codex `mcp_servers.<name>` config overrides.
- For a proxy-managed Docker sandbox, the proxy rewrites loopback server URLs
  to the sandbox host gateway before the configuration crosses to the split,
  the same way it maps the OTLP endpoint. The stored configuration keeps the
  URL the caller supplied.
- Header values are stored with the harness configuration and are not
  redacted from projections; callers must treat them as they would any other
  configured secret and rotate them by recreating the harness.
