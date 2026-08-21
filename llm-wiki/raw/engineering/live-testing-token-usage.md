# Live harness testing token-usage addendum

Snapshot date: 2026-08-21

This addendum snapshots the token-usage rule added to `docs/live-testing.md`.
It supplements the earlier live-testing source without rewriting it.

For each provider, the shared live gate must observe a canonical
`usage_updated` event before the authoritative terminal event for both the
successful create and resume turns. Every populated token field must be a
nonnegative integer, and at least one populated field must be positive.
Provider-native categories that are not reported remain absent.

The assertion is mandatory whenever a provider's opt-in live gate is enabled.
It does not add another model turn or print token values.
