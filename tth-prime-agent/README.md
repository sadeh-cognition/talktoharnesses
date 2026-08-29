# tth-prime-agent

prime_agent harness split service for talktoharnesses.

A thin Django + django-ninja HTTP/SSE wrapper around this kind's
`HarnessAdapter`, speaking the shared `tth_types.split_api` contract to
tth-proxy. Run locally: `make serve` (port 8116). Health: `GET /v1/health`.
