# tth-codex

codex harness split service for talktoharnesses.

A thin Django + django-ninja HTTP/SSE wrapper around this kind's
`HarnessAdapter`, speaking the shared `tth_types.split_api` contract to
tth-proxy. Run locally: `make serve` (port 8113). Health: `GET /v1/health`.

Git-backed workspace-write sessions use a Codex permissions profile extending
`:workspace`, with write access to the repository's resolved Git directory and
common directory. This permits commits in linked worktrees without making their
parent directories writable. `.agents` and `.codex` protections and the default
network denial remain inherited. Read-only and full-access modes are unchanged;
`yolo` still controls approvals only. The Project's outer Docker mounts,
credential isolation, and egress policy do not change.

`tests/harness/test_permissions.py` runs the pinned CLI's actual sandbox without
model calls: ordinary and linked-worktree commits succeed while protected-path,
outside-workspace, and direct-network attempts fail.
