# TTH-Owned Harness Executable Discovery Requirements

Product input approved on 2026-08-21.

## Intent

TalkToHarnesses (TTH) owns discovery of process-bound harness executables.
Callers select a harness kind and provide provider-neutral configuration; they
do not find or supply executable names or paths.

## Discovery

- At probe and launch, TTH resolves Grok, Cursor, OpenCode, and Prime Agent
  from a TTH-owned process environment override or the conventional executable
  name on TTH's PATH.
- The TTH environment override takes precedence. If it is present but invalid,
  discovery fails rather than falling back to PATH.
- Existing executable existence, type, permission, and ownership validation
  applies to the discovered path.
- Codex and Claude remain SDK-managed.

## Configuration contract

- `executable_path` is not part of the HTTP or domain harness configuration.
- New requests containing `executable_path` are rejected.
- Stored harness or binding configuration containing `executable_path` is
  rejected and must be recreated; TTH does not migrate or silently ignore it.
- A launch snapshot may record the executable path that TTH resolved and used.

## Supersession

This source supersedes the statement in `raw/product/readme.md` that TTH never
discovers external executables and the former optional `executable_path`
configuration contract. The older source remains preserved as historical
product input.

## Exclusions

- Do not install or upgrade external harness binaries.
- Do not add per-harness executable paths or provider-native flags.
- Do not add executable discovery to callers such as Agentbahn.

## Acceptance criteria

1. Process-bound harness probe and launch discover the executable inside TTH
   using the documented environment-then-PATH precedence.
2. Missing, invalid, unowned, or non-executable discoveries fail through the
   existing TTH executable error boundary.
3. TTH's HTTP and domain harness configuration reject `executable_path`,
   including historical stored JSON.
4. SDK-managed harness behavior and provider-neutral harness configuration are
   unchanged.
