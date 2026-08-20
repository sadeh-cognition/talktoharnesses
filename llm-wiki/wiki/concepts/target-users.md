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
last_verified: 2026-08-20
---

# Target Users

TalkToHarnesses is for software developers who need a single, persistence-backed interface to local coding-agent CLIs.

## Library embedders

Python applications import `TalkToHarnessesService` and provider adapters without Django. They supply their own persistence and process composition.

## Django hosts

Applications add `talktoharnesses.django` to `INSTALLED_APPS`, wrap ASGI with `talktoharnesses_lifespan`, and mount `/api/v1/`. They issue JWTs for authenticated users. Harness processes run as the Django OS user; this is not a sandbox.

## Remote HTTP clients

Hosted products consume the official HTTP client and treat TalkToHarnesses as the authority for conversations, transcripts, approvals, and provider execution.

## Related

- [TalkToHarnesses overview](../overview.md)
- [Product overview](../maps/product-overview.md)
- [Official HTTP client](../capabilities/official-http-client.md)
