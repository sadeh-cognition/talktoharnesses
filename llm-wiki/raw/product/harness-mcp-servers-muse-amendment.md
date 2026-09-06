# Harness MCP servers Muse Code amendment

Approved: 2026-09-06

Amends the harness MCP servers requirement: Muse Code supports configured MCP
servers and joins Claude Code, Cursor, Grok, and Codex as a supporting kind.

- Muse Code reads MCP servers from its settings file at
  `$XDG_CONFIG_HOME/muse/settings.json` and offers no per-session path over
  the Muse Session Protocol, so the split gives each `muse serve` host whose
  harness names servers a private configuration directory: the host's saved
  settings with the servers merged into `mcp_servers` in Muse's
  `streamable_http` shape, and links to the host's `auth.json` and
  `trust.json`. The directory is created at spawn and removed when the
  adapter closes.
- Harnesses without servers keep using the host's ordinary configuration.
- Servers named in the harness replace same-named saved entries; other saved
  servers remain.
- Muse Code documents that MCP tools run outside its filesystem and network
  sandbox while approval still applies; that posture is unchanged by this
  amendment.
- OpenCode and Prime Agent remain unsupported and keep rejecting configured
  servers.
