# Split Service Runtime Ownership Requirements

Product input approved on 2026-08-29.

## Intent

TalkToHarnesses uses a proxy plus one independently configured split service
per harness kind. The client-facing API and provider-neutral harness
configuration remain unchanged.

## Runtime ownership

- The proxy drives every harness kind through a remote split adapter.
- Operators configure a kind with `TTH_SPLIT_<KIND>_URL` or enable its
  proxy-managed Docker sandbox through `TTH_SANDBOX_KINDS`.
- Missing split configuration fails closed; it does not fall back to an
  in-process provider adapter.
- Each split owns its provider adapter, CLI or SDK discovery, provider
  authentication, compatibility floor, and supervised harness process.
- Executable environment overrides apply to the split process, not the proxy.

## Deployment boundary

- A URL override can point to a locally or remotely operated split; its process
  isolation and credentials belong to that deployment.
- For a managed Docker split, the proxy builds no provider command line. It
  starts and reuses the kind's container with the documented mounts, credential
  forwarding, resource limits, and split token.
- Client JWT authentication is separate from proxy-to-split authentication and
  does not imply that a harness runs as the proxy's Django OS user.

## Compatibility documentation

- Compatibility JSON remains owned by each split project.
- The repository-level `SUPPORTED_HARNESSES.md` is an aggregate generated from
  all split-owned compatibility documents.

## Supersession

This source supersedes the proxy-side runtime ownership and executable
discovery statements in `raw/product/readme.md` and
`raw/product/tth-owned-harness-executable-discovery-requirements.md`. Those
sources remain preserved as historical product input. The provider-neutral
configuration contract, including rejection of `executable_path`, remains in
force.

## Acceptance criteria

1. Every configured harness kind resolves to a remote split service.
2. An unconfigured kind fails without launching a provider in the proxy.
3. CLI/SDK discovery and provider execution occur in the split service.
4. Operators can choose an explicit split URL or a proxy-managed Docker split.
5. The support matrix is reproducibly aggregated from split-owned compatibility
   documents.
