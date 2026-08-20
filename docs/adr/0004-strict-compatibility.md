# ADR 0004: Strict Compatibility

- **Status:** Superseded by [ADR 0007](0007-floor-and-probe-compatibility.md)
- **Date:** 2026-08-07

## Context

Soft-fail adapters that claim support for harnesses they cannot drive produce
silent production failures. Consumers need an explicit, versioned contract for
what is supported.

## Decision

Compatibility is strict and explicit. Adapters must not claim unsupported
harnesses. This package remains a pre-release (`*.dev0`) until the five-harness
milestone is complete. Future phases introduce `SUPPORTED_HARNESSES` and related
manifests as the public contract.

## Consequences

Missing provider dependencies surface as hard, documented errors—not partial
success. Compatibility documents and manifests are deferred until their
implementing phases.
