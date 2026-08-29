---
type: concept
title: Target Users
status: maintained
audiences:
  - product
  - developer
tags:
  - type/concept
  - audience/product
last_verified: 2026-08-30
verified_against_commit: 2920f5820783245bd5b871e61edd44642ebfe56a
---

# Target Users

TalkToHarnesses is for software developers who need a single, persistence-backed interface to local coding-agent CLIs.

## Library embedders

Python applications import `TalkToHarnessesService` without Django. They supply their own persistence and adapter registry, using remote split adapters or another protocol-compatible implementation.

## Django hosts

Applications add `talktoharnesses.django` to `INSTALLED_APPS`, wrap ASGI with `talktoharnesses_lifespan`, and mount `/api/v1/`. They issue JWTs for authenticated users; harness kinds need no enablement configuration because the proxy spawns each kind's managed Docker sandbox on demand. Provider execution belongs to the split deployment, not the Django proxy process.

## Remote HTTP clients

Hosted products consume the official HTTP client and treat TalkToHarnesses as the authority for conversations, transcripts, approvals, and provider execution.

## Related

- [TalkToHarnesses overview](../overview.md)
- [Product overview](../maps/product-overview.md)
- [Official HTTP client](../capabilities/official-http-client.md)
