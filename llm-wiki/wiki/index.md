---
type: index
title: Wiki Index
status: maintained
audiences:
  - product
  - developer
tags:
  - type/index
last_verified: 2026-08-20
---

# Wiki Index

Use this catalog to route into the maintained knowledge graph. Obsidian users should normally begin at [TalkToHarnesses Knowledge Base](../Home.md). Agentbahn opens this page first.

## Maps of Content

- [Product overview](maps/product-overview.md): capabilities, journeys, requirements, and status.
- [Developer overview](maps/developer-overview.md): architecture, interfaces, decisions, operations, and evidence.
- [Requirements by status](maps/requirements-by-status.md): plugin-free delivery view.
- [Architecture and integrations](maps/architecture-and-integrations.md): system structure and external boundaries.
- [Compatibility and adapters](maps/compatibility-and-adapters.md): floors, probes, and provider adapters.
- [HTTP API map](maps/http-api.md): authenticated HTTP/SSE clusters.

## Capabilities

- [Unified harness adapters](capabilities/unified-harness-adapters.md)
- [Persistent conversations and turns](capabilities/persistent-conversations.md)
- [Approvals and structured questions](capabilities/approvals-and-questions.md)
- [Django HTTP and SSE surface](capabilities/django-http-sse.md)
- [Floor-and-probe compatibility](capabilities/floor-and-probe-compatibility.md)
- [Search, retention, and transcripts](capabilities/search-retention-transcripts.md)
- [Isolated harness runtimes](capabilities/isolated-harness-runtimes.md)
- [Official HTTP client](capabilities/official-http-client.md)

## Requirements and journeys

- [Probe and configure harnesses](requirements/probe-and-configure-harnesses.md)
- [Create and manage conversations](requirements/create-and-manage-conversations.md)
- [Submit turns and stream events](requirements/submit-turns-and-stream-events.md)
- [Resolve approvals and structured questions](requirements/resolve-approvals-and-structured-questions.md)
- [Steer, interrupt, and switch harness](requirements/steer-interrupt-and-switch-harness.md)
- [Queue and edit prompts](requirements/queue-and-edit-prompts.md)
- [Apply approval rules](requirements/apply-approval-rules.md)
- [Search conversations](requirements/search-conversations.md)
- [Retain and prune transcripts](requirements/retain-and-prune-transcripts.md)
- [Export and import transcripts](requirements/export-and-import-transcripts.md)
- [Authenticate with JWT](requirements/authenticate-with-jwt.md)
- [Host Django ASGI with readiness](requirements/host-django-asgi-with-readiness.md)
- [Host Django and run a conversation](journeys/host-django-and-run-a-conversation.md)
- [Resolve a pending approval](journeys/resolve-a-pending-approval.md)
- [Search conversations and apply retention](journeys/search-conversations-and-apply-retention.md)

## Technical design

- [TalkToHarnesses overview](overview.md)
- [Target users](concepts/target-users.md)
- [System context](architecture/system-context.md)
- [Technology stack](architecture/technology-stack.md)
- [Layered architecture](architecture/layered-architecture.md)
- [Provider adapters](architecture/provider-adapters.md)
- [Runtime isolation architecture](architecture/runtime-isolation.md)
- [Persistence and event sequencing](architecture/persistence-and-event-sequencing.md)
- [Observability](architecture/observability.md)
- [HTTP and SSE API](interfaces/http-and-sse-api.md)
- [Python application facade](interfaces/python-application-facade.md)
- [Adapter protocol](interfaces/adapter-protocol.md)
- [Official HTTP client interface](interfaces/official-http-client.md)
- [Conversation](domain/conversation.md)
- [Harness instance](domain/harness-instance.md)
- [Turn and command](domain/turn-and-command.md)
- [Interaction](domain/interaction.md)
- [Conversation event](domain/conversation-event.md)
- [Persistence decision](decisions/persistence.md)
- [Event sequencing decision](decisions/event-sequencing.md)
- [Runtime isolation decision](decisions/runtime-isolation.md)
- [Strict compatibility decision](decisions/strict-compatibility.md)
- [JWT authentication decision](decisions/jwt-authentication.md)
- [Retention decision](decisions/retention.md)
- [Floor-and-probe compatibility decision](decisions/floor-and-probe-compatibility.md)
- [Development guidelines](operations/development-guidelines.md)
- [Testing guidelines](operations/testing-guidelines.md)
- [Deployment](operations/deployment.md)
- [Upgrading](operations/upgrading.md)
- [Releasing](operations/releasing.md)
- [Performance gates](operations/performance-gates.md)
- [Wiki maintenance](operations/wiki-maintenance.md)
- [Glossary](glossary.md)

## Analyses

No durable analysis pages have been created yet.

## Related

- [TalkToHarnesses Knowledge Base](../Home.md)
- [Wiki log](log.md)
