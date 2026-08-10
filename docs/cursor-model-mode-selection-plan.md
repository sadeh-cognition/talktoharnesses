# Cursor ACP Model, Parameter, and Mode Selection

## 1. Goal and Required Behavior

Update the Cursor adapter for the pinned Cursor Agent release `2026.08.04-aaa8809` so callers can select:

- A Cursor model family.
- Model-specific parameters such as reasoning/effort, context, Fast, Thinking, and Max when Cursor advertises them.
- The Cursor workflow mode: `agent`, `plan`, or `ask`.
- A one-turn model override through the existing `TurnRequest.model`.

Use Cursor ACP's standard `session/set_config_option` method and Cursor's `_meta.parameterizedModelPicker` initialization extension. Do not use Cursor CLI startup flags for these selections.

The change must preserve these public interfaces:

- `HarnessConfiguration.model: str | None`
- `HarnessConfiguration.mode: str | None`
- `TurnRequest.model: str | None`
- `HarnessSession.model` and `HarnessSession.mode`

No database migration, Django API schema change, or new provider-neutral configuration field is required.

### Model selector syntax

Accept the selector in the existing `model` string:

```text
<model-id>
<model-id>[<parameter-id>=<value>,...]
```

Examples:

```text
composer-2.5
composer-2.5[fast=false]
gpt-5.6-sol[context=272k,reasoning=high,fast=false]
claude-opus-5[thinking=true,context=1m,effort=max,fast=false]
auto
```

Rules:

- Trim surrounding whitespace from the complete selector, model ID, parameter IDs, and values.
- The model ID must remain non-empty after trimming.
- A selector may contain at most one bracketed parameter section.
- `]` must be the final non-whitespace character.
- Empty brackets, as in `default[]`, mean no explicit parameters.
- Each non-empty parameter entry must contain exactly one `=`.
- Parameter IDs and values must be non-empty.
- Duplicate parameter IDs are invalid.
- Commas and brackets are not supported inside parameter values.
- Parameter names and values are case-sensitive.
- Normalize model ID `auto` to Cursor ACP model value `default`.
- Preserve parameter order for deterministic ACP calls and tests.
- Do not hardcode a list of models or parameter values. Validate them against the current session's advertised ACP options.

Malformed syntax must raise `DomainError(ErrorCode.PROVIDER_INCOMPATIBLE, ...)`.

### Selection lifetime

- `HarnessConfiguration.model` and `mode` establish the session baseline on both create and resume.
- If no configured model is supplied, capture Cursor's current model and all advertised parameter values as the baseline.
- If no configured mode is supplied, preserve Cursor's current workflow mode.
- `TurnRequest.model` overrides the model for that turn.
- The next turn without an override restores the captured session baseline, matching Prime Agent's existing behavior.
- A model override never changes the workflow mode.
- Apply configuration before calling `CursorNormalizer.begin_turn()` and before sending `session/prompt`.
- A configuration failure must therefore fail before Cursor receives the user prompt.

## 2. ACP Schema and Protocol Changes

### Cursor configuration schemas

In `src/talktoharnesses/providers/acp/schemas/cursor_ext.py`, add strict frozen Pydantic models for the pinned Cursor configuration shapes.

Define a configuration option value with:

- `name: str`
- `value: str`
- Optional `description: str | None`

Define a select configuration option with:

- `id: str`
- `category: str`
- `type: Literal["select"]`
- `currentValue: str`
- `options: tuple[CursorConfigOptionValue, ...]`
- Optional `name: str | None`
- Optional `description: str | None`

Use aliases only where required by the actual camelCase ACP payload. Keep `extra="forbid"`, `frozen=True`, and strict validation, consistent with the existing ACP schemas.

Add one parsing helper that:

1. Requires an object result.
2. Requires a `configOptions` list.
3. Strictly validates every option in that list.
4. Returns an immutable tuple of validated options.
5. Raises `DomainError(ErrorCode.PROTOCOL_ERROR, ...)` for malformed provider data.

Do not model unrelated top-level `session/new` or `session/load` fields. Existing session-ID parsing remains the source of truth for `sessionId`.

### Cursor-only outbound method

Do not add `session/set_config_option` to the shared `ALLOWED_OUTBOUND_METHODS`, because that would silently broaden the Grok adapter.

Instead:

- Define a Cursor outbound-method set equal to the shared ACP methods plus `session/set_config_option`.
- Make `cursor_acp_protocol()` use that set.
- Leave `grok_acp_protocol()` unchanged.
- Update Cursor's initialize-identity check to compare `required_agent_methods` against the Cursor protocol's outbound set instead of the shared base constant.

Add a protocol test proving:

- A connection using `cursor_acp_protocol()` can send `session/set_config_option`.
- A connection using the default/Grok protocol rejects that method with `unsupported_native_event`.

### Initialization capability

Change `CursorAdapter._initialize()` so `clientCapabilities` is:

```python
{
    "_meta": {
        "parameterizedModelPicker": True,
    }
}
```

Do not advertise filesystem, terminal, boolean-config, or other client capabilities. The adapter has no corresponding reverse handlers, and they are outside this change.

Add a test that decodes the actual initialize request written to the fake process and asserts the exact `_meta` capability.

## 3. Cursor Adapter Implementation

### Internal model-selection type

In `src/talktoharnesses/providers/cursor/adapter.py`, add one private frozen dataclass:

```python
@dataclass(frozen=True, slots=True)
class _CursorModelSelection:
    model_id: str
    parameters: tuple[tuple[str, str], ...] = ()
```

Keep all parsing and application helpers private to the Cursor adapter. Do not create a shared configuration framework because no second adapter currently uses Cursor's syntax.

Add private adapter state:

```python
self._config_options: tuple[CursorSelectConfigOption, ...] = ()
self._session_model_selection: _CursorModelSelection | None = None
self._current_model_selection: _CursorModelSelection | None = None
```

Reset these fields only when constructing a fresh adapter instance; one adapter already represents one conversation runtime.

### Selector parser

Add `_parse_model_selector(value: str) -> _CursorModelSelection`.

It must implement the grammar above, including `auto` -> `default`. Error details may contain the invalid selector or parameter ID, but must not contain unrelated provider output.

Add a formatter only if tests or diagnostics require one. Do not build a general parser abstraction.

### Configuration lookup helpers

Add small private helpers for:

- Finding a configuration option by `id`.
- Checking whether a requested value appears in that option's `options`.
- Identifying model parameters by category.

Only options with these categories may be supplied inside the model brackets:

```text
model_config
thought_level
```

Reject `model`, `mode`, or any unrelated future session option inside a model selector.

Add a helper that captures the effective model state from the current option list:

1. Read the current value of config option `model`.
2. Collect every option whose category is `model_config` or `thought_level`.
3. Record each option's `id` and `currentValue` in provider response order.
4. Return `_CursorModelSelection(model_id, parameters)`.

This captured complete state is needed to restore the session baseline after a turn override.

### Setting one ACP option

Add:

```python
async def _set_config_option(
    self,
    *,
    session_id: str,
    config_id: str,
    value: str,
    options: tuple[CursorSelectConfigOption, ...],
) -> tuple[CursorSelectConfigOption, ...]:
```

Behavior:

1. Find `config_id` in the current option list.
2. If absent, raise `provider_incompatible`.
3. Confirm `value` is one of the option's advertised values.
4. If not advertised, raise `provider_incompatible` with:
   - `config_id`
   - requested value
   - advertised values
5. Send:

```json
{
  "sessionId": "<session-id>",
  "configId": "<config-id>",
  "value": "<value>"
}
```

using `session/set_config_option`.

6. Await the response before sending another configuration request.
7. Convert `JsonRpcRemoteError` into `DomainError(PROVIDER_INCOMPATIBLE, ...)`, retaining only the remote numeric code and affected config ID in details.
8. Strictly parse the returned complete `configOptions` list.
9. Require the returned option's `currentValue` to equal the requested value.
10. Treat a malformed response or mismatched current value as `protocol_error`.
11. Return the new option tuple, which becomes the source of truth for the next setter call.

Never continue using stale configuration options after a successful setter response.

### Applying a model selector

Add a helper that receives a parsed `_CursorModelSelection` and current options.

Apply it in this exact order:

1. Validate and set config option `model` to the normalized base model ID.
2. Use the setter response to obtain the parameter options for that model.
3. For every explicitly supplied parameter, in selector order:
   - Require a matching config option.
   - Require category `model_config` or `thought_level`.
   - Validate and set its value.
   - Replace the current option tuple with the setter response.
4. Capture and return the complete effective model state after all explicit parameters are applied.

Omitted parameters retain the values Cursor advertises after selecting the model.

When restoring a previously captured baseline, apply every stored baseline parameter. This guarantees that a prior turn override cannot leak reasoning, context, Thinking, Fast, or Max settings into later turns.

### Applying session configuration

Add a helper invoked by both `start()` and `resume()` after obtaining the native session ID.

Inputs:

- Native session ID.
- `session/new` or `session/load` result.
- Configured model selector.
- Configured workflow mode.

Required sequence:

1. Parse and store the returned `configOptions`.
2. Require both `model` and `mode` options to exist for the pinned Cursor release.
3. If a configured model exists:
   - Parse it.
   - Apply the base model and explicit parameters.
4. If a configured mode exists:
   - Validate it against the advertised `mode` option.
   - Set it after all model options.
5. Capture the complete resulting model state.
6. Assign it to both `_session_model_selection` and `_current_model_selection`.
7. Save the final option tuple in `_config_options`.

If the pinned release omits the model or mode options entirely, raise `provider_incompatible`; this means the runtime no longer provides a capability claimed by the compatibility record.

Do not set a model or mode when the corresponding configuration field is `None`.

Only call `self._normalizer.set_session(...)` and construct the returned `HarnessSession` after configuration succeeds.

Keep `HarnessSession.model` and `.mode` equal to the caller's configured strings rather than replacing them with normalized ACP values.

### Per-turn model override

At the start of `submit()` and before `begin_turn()`:

- If `request.model` is not `None`, parse and apply it.
- Otherwise, if `_current_model_selection` differs from `_session_model_selection`, restore the complete session baseline.
- Update `_current_model_selection` and `_config_options` after every successful change.
- Skip all configuration calls when the exact effective state is already active.
- Do not update `_session_model_selection` for a turn override.

After configuration succeeds, retain the existing prompt submission and prompt watcher behavior unchanged.

### Process arguments

Simplify `src/talktoharnesses/providers/cursor/argv.py`:

```python
def build_cursor_argv() -> tuple[str, ...]:
    return ("acp",)
```

Then update:

- `CursorAdapter.build_argv()` to call it without model or mode.
- `CursorAdapter.probe()` to stop calling argv construction for model/mode validation.

Do not pass `--model` or `--mode` to Cursor's process. ACP remains the only selection path.

## 4. Compatibility Data and Documentation

Update `src/talktoharnesses/data/compatibility/cursor.json` for release `cursor-2026.08.04-aaa8809`:

- Add `session/set_config_option` to `required_agent_methods`.
- Add `clientCapabilities._meta.parameterizedModelPicker` to `allowlisted_extensions`.
- Update the release note to state that create/resume, permissions, model-family selection, parameter selection, and Agent/Plan/Ask selection are live-proven on Linux.

Keep:

- The release ID unchanged.
- The CLI version unchanged.
- ACP protocol version `1`.
- The existing create/resume matrices unchanged.
- Adapter/package version `2026.8.1` unchanged unless the repository's release workflow separately requires a version bump.

Run the generated support-document command after changing compatibility data. Commit a `SUPPORTED_HARNESSES.md` change only if the renderer produces one; never edit generated tables manually.

The new plan document is the detailed reference for selector syntax. Do not expand the generic Django API or add provider-specific fields to shared API documentation.

## 5. Test Fixtures and Unit Tests

### Fake ACP process

Update the shared `_FakeAcpProcess` in `tests/contract/fakes.py` without changing Grok behavior.

For fake Cursor processes only:

- Record decoded requests so tests can inspect initialization and setter order.
- Return `configOptions` from both `session/new` and `session/load`.
- Advertise at least:
  - Models: `default`, `composer-2.5`, `gpt-5.6-sol`
  - Modes: `agent`, `plan`, `ask`
- Advertise dynamic parameters:
  - `composer-2.5`: `fast=false|true`
  - `gpt-5.6-sol`:
    - `context=272k|1m`
    - `reasoning=low|medium|high`
    - `fast=false|true`
- Reset parameter options and their defaults when the model changes.
- Validate `session/set_config_option` requests.
- Return the complete updated option list after every successful setter.
- Return a JSON-RPC error for unknown config IDs or values.

Keep the simpler existing responses for Grok.

Update the common contract's Cursor configuration to use a valid non-empty model and mode, such as:

```python
model="composer-2.5[fast=false]"
mode="ask"
```

Ensure the Cursor launch snapshot uses matching values where the contract expects configuration and launch metadata to agree.

### Cursor adapter tests

Add focused tests covering:

1. Simple model parsing.
2. Parameterized model parsing.
3. Whitespace normalization.
4. `auto` -> `default`.
5. Empty brackets.
6. Missing model ID.
7. Missing closing bracket.
8. Trailing characters after `]`.
9. Nested or repeated bracket sections.
10. Parameter without `=`.
11. Empty parameter ID.
12. Empty parameter value.
13. Duplicate parameter IDs.
14. Initialize request contains only the expected `_meta` capability.
15. Start applies model, every explicit parameter, then mode.
16. Resume applies the same authoritative configuration.
17. No configured values produce no setter calls but still capture Cursor's baseline.
18. Unknown model fails before session return.
19. Unknown mode fails before session return.
20. Unknown parameter ID fails after model selection and before prompting.
21. Known parameter with an unsupported value fails.
22. Setter JSON-RPC rejection maps to `provider_incompatible`.
23. Malformed setter response maps to `protocol_error`.
24. Setter response with the wrong `currentValue` maps to `protocol_error`.
25. A turn override is applied before `session/prompt`.
26. A second turn without an override restores every baseline parameter.
27. Repeating the already-active effective model does not issue redundant setters.
28. A configuration failure does not call `begin_turn()` and does not send `session/prompt`.
29. `build_cursor_argv()` always returns exactly `("acp",)`.

Keep existing permission, interrupt, prompt-terminal, and close tests unchanged except for fixture adjustments needed by the new session metadata.

### Compatibility and protocol tests

Update compatibility assertions to verify:

- `session/set_config_option` is required.
- The parameterized-picker extension is recorded.
- Model and mode no longer cause argv rejection.

Add the Cursor/Grok allowlist-isolation test described above.

No test should depend on a hardcoded production list of all Cursor models.

## 6. Live Verification

Extend `tests/live/test_cursor_live.py` using the existing opt-in environment gate.

Use a configuration equivalent to:

```python
HarnessConfiguration(
    kind=HarnessKind.CURSOR,
    executable_path=executable,
    working_directory=str(tmp_path),
    model="composer-2.5[fast=false]",
    mode="ask",
)
```

The live test must prove:

1. Probe still matches `2026.08.04-aaa8809`.
2. `session/new` accepts the configured model, `fast=false`, and Ask mode.
3. A prompt completes.
4. A one-turn override to `composer-2.5[fast=true]` is accepted.
5. A later unoverridden prompt restores `fast=false`.
6. Resume reapplies or preserves the configured baseline and completes another prompt.
7. Interaction, interrupt, deduplication, and shutdown behavior remain intact.

The test does not need to inspect Cursor's dashboard. Successful setter responses whose `currentValue` matches the request are the protocol-level assertion.

Run it explicitly with:

```bash
TALKTOHARNESSES_LIVE_CURSOR=1 \
TALKTOHARNESSES_CURSOR_EXECUTABLE=/absolute/path/to/cursor-agent \
uv run pytest tests/live/test_cursor_live.py -q -s
```

## 7. Verification Order and Completion Criteria

Implement and verify in this order:

1. Add Cursor configuration schemas and parser tests.
2. Add the Cursor-only outbound method.
3. Add initialization metadata.
4. Implement session configuration for create.
5. Reuse the same helper for resume.
6. Add baseline/current model tracking.
7. Add per-turn overrides and restoration.
8. Simplify Cursor argv handling.
9. Update compatibility data and fakes.
10. Extend live coverage.
11. Regenerate derived documentation.
12. Run all quality gates.

Required commands:

```bash
uv run pytest tests/unit/providers/cursor tests/unit/providers/acp/test_connection.py -q
uv run pytest tests/contract/test_common_adapter_contract.py -q
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run python -m talktoharnesses.providers.render_supported
DJANGO_SETTINGS_MODULE=tests.django_settings uv run django-admin makemigrations --check --dry-run
git diff --check
```

The work is complete only when:

- Cursor configurations no longer reject non-empty model or mode values at argv construction.
- Create and resume apply and verify model, parameters, and workflow mode through ACP.
- Per-turn overrides reset on the next unoverridden turn.
- Invalid selections fail before `session/prompt`.
- Grok's allowlist remains unchanged.
- Existing provider contracts and full tests pass.
- No migration or provider-specific public API field is introduced.
