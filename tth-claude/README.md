# tth-claude

Claude Code harness split service for talktoharnesses.

A thin Django + django-ninja HTTP/SSE wrapper around the Claude `HarnessAdapter`.
The tth-proxy talks to this service over the shared `tth_types.split_api` contract;
this service owns everything Claude-specific: the adapter, normalizer, probe, and
compatibility floor. Sessions are in-memory — a restart drops live sessions and the
proxy recovers via native session resume.

Run locally: `make serve` (port 8114). Health: `GET /v1/health`.
Set `TTH_SPLIT_TOKEN` to require the `X-TTH-Split-Token` header on every request.
