---
type: source
title: Project sandbox policy request
status: source
audiences: [product, developer]
tags: [type/source]
last_verified: 2026-09-20
---

# Project sandbox policy request

On 2026-09-20 the user requested default-deny egress, credential proxying,
restricted push scope, and a command blocklist, implemented in new worktrees.
The user authorized implementation. Project policies apply to new sessions;
existing sessions retain their revision. Host publication uses a saved remote,
the assigned branch, and the recorded commit. Command checks cover intercepted
tool calls and are not an OS execution boundary. Provider credentials remain
outside agent containers. Unsupported credential methods fail closed.
