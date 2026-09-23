---
type: requirement
title: Probe and Configure Harnesses
status: implemented
audiences:
  - product
  - developer
tags:
  - type/requirement
  - capability/adapters
  - status/implemented
last_verified: 2026-09-06
verified_against_commit: d55a5a38d39b80e5f755d419b52b680bb1c08347
sources:
  - raw/product/readme.md
  - raw/product/tth-owned-harness-executable-discovery-requirements.md
  - raw/product/split-service-runtime-ownership.md
  - raw/product/on-demand-sandbox-provisioning.md
  - raw/product/harness-mcp-servers-requirements.md
  - raw/product/harness-mcp-servers-muse-amendment.md
  - raw/engineering/adr-0007-floor-and-probe-compatibility.md
---

# Probe and Configure Harnesses

## Intent

An owner can create a named harness of a supported kind, store its configuration, probe the configured split against its packaged floor, and read advertised models, modes, efforts, and capability flags.

## Current behavior

`POST /harnesses` persists an owner-owned `HarnessInstance` with kind, working directory, workspace roots, optional model, mode, effort, and yolo, and optional `mcp_servers`: streamable HTTP MCP servers (unique short name, absolute `http(s)` URL, optional headers) that supporting splits attach to every new and resumed session. Claude Code passes them as the Agent SDK `mcp_servers` option, Cursor and Grok as ACP `mcpServers` on `session/new` and `session/load`, Codex as `mcp_servers.<name>` config overrides, and Muse Code through a private per-host `XDG_CONFIG_HOME` whose `settings.json` merges the servers into `mcp_servers` in Muse's `streamable_http` shape and links the host's `auth.json` and `trust.json` (created at spawn, removed on close, per the [Muse amendment](../../raw/product/harness-mcp-servers-muse-amendment.md)); OpenCode and Prime Agent fail probe, start, and resume with `provider_incompatible` when any server is configured. Each split publishes `supports_mcp_servers` in its capability flags. For a proxy-managed sandbox the `RemoteHarnessAdapter` rewrites loopback server URLs to the sandbox host gateway before probe and session requests cross to the split, leaving the stored configuration untouched. A policy sandbox instead receives credential-free gateway URLs that a host relay maps back to the stored URL and headers (see [Project sandbox policies](project-sandbox-policies.md)). The create body and domain configuration reject executable paths. The proxy constructs a `RemoteHarnessAdapter` for every kind and spawns the kind's Docker sandbox on demand when its endpoint is first resolved: a missing image is built locally from the repository's per-kind build context, and the spawned sandbox is recorded in the proxy database (kind, container, image, port, base URL, split token, status) so a restarted proxy reattaches with the persisted token. There is no per-kind enablement configuration and no in-process provider fallback. While a build or boot is still in progress a request fails with the retryable `sandbox_preparing`; unrecoverable sandbox failures surface as `sandbox_unavailable` with a fixed-vocabulary actionable message. The split owns CLI or SDK discovery, provider authentication, compatibility data, and process supervision. `POST /harnesses/{id}/probe` returns `HarnessProbeProjection` including `VersionAdvisory`. Identities below the split-owned floor or on unpublished platforms fail with `provider_incompatible`. Models, modes, and efforts come from the live split. Cursor accepts `model-id[key=value,...]` selectors. `yolo` is fixed at creation. Historical stored JSON containing `executable_path` fails validation and must be recreated.

Muse Code is available as `muse` with a Linux floor of `1.0.3-R2198.1`.
Probe compares the numeric R-build identity, verifies MSP v1 and durable sessions,
and queries the live model catalog. Mode and effort lists are empty.

## Gap

No gap remains against the documented floor-and-probe contract.

## Acceptance criteria

- Creating a harness stores kind, working directory, workspace roots, optional model, mode, effort, yolo, and MCP servers without an executable path or invented CLI flags.
- MCP servers accept only absolute `http(s)` URLs with unique names; supporting splits attach them in the provider-native shape, unsupported splits fail with `provider_incompatible`, and loopback URLs reach a managed sandbox rewritten to its host gateway.
- HTTP, direct domain construction, and stored configuration reject `executable_path`.
- Every kind resolves through a proxy-managed Docker sandbox spawned on demand, with the image built locally when missing.
- Spawned sandboxes are recorded in the proxy database, and a restarted proxy reattaches to running containers using the persisted split token.
- Requests during sandbox preparation fail with the retryable `sandbox_preparing`; sandbox failures surface actionable `sandbox_unavailable` messages.
- Process-bound splits resolve their executable from the split environment override or conventional name on the split's PATH.
- Probe rejects identities older than the floor or on unpublished platforms.
- Probe accepts newer identities and reports an advisory vs `latest_verified`.
- Capabilities, models, and modes endpoints return the last probed or freshly probed values.
- Missing split dependencies and malformed version output fail closed.

## Implementation evidence

- `tth-muse/src/tth_muse/harness/` (Muse Code MSP integration)

- `src/talktoharnesses/application/service.py` (`create_harness`, `probe_harness`, `get_harness_capabilities`, `get_harness_models`, `get_harness_modes`)
- `src/talktoharnesses/remote/registry.py` (`build_remote_adapter_registry`)
- `src/talktoharnesses/remote/sandbox.py` (`SandboxConfig`, `SandboxManager.endpoint`)
- `tth-*/src/tth_*/harness/probe.py`
- `src/talktoharnesses/runtime/manager.py` (`_plan_launch`)
- `src/talktoharnesses/domain/models.py` (`HarnessConfiguration`)
- `tth-*/src/tth_*/harness/compatibility.py`
- `tth-*/src/tth_*/data/compatibility/*.json`
- `src/talktoharnesses/django/api/routes.py`

## Test evidence

- `tth-muse/tests/test_muse.py`
- `tests/live/test_muse_sandbox_live.py`

- `tests/unit/remote/test_sandbox_and_registry.py`
- `tth-*/tests/harness/test_probe.py`
- `tth-*/tests/harness/test_compatibility.py`
- `tth-*/tests/runtime/test_paths.py`
- `tests/unit/django/test_api.py` (`test_create_harness_rejects_executable_path`)
- `tests/unit/django/test_sandbox_store.py`
- `tests/live/test_*_sandbox_live.py`
- `tests/test_render_supported.py`

## Related

- [Unified harness adapters](../capabilities/unified-harness-adapters.md)
- [Floor-and-probe compatibility](../capabilities/floor-and-probe-compatibility.md)
- [Floor-and-probe compatibility decision](../decisions/floor-and-probe-compatibility.md)
- [README product source](../../raw/product/readme.md)
- [Approved executable-discovery requirements](../../raw/product/tth-owned-harness-executable-discovery-requirements.md)
- [Approved split runtime ownership](../../raw/product/split-service-runtime-ownership.md)
- [Approved on-demand sandbox provisioning](../../raw/product/on-demand-sandbox-provisioning.md)
