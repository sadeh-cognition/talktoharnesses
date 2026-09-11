"""RTK rewrite hook for the Claude SDK ``PreToolUse`` event.

RTK (https://github.com/rtk-ai/rtk) rewrites Bash commands to ``rtk <cmd>``
so the model reads trimmed output. The split runs with ``setting_sources=[]``
so the usual settings.json hook never loads; shell out to the same hook
processor in-process instead. Everything fails open: no rtk, no rewrite.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

RTK_HOOK_TIMEOUT_SECONDS = 2.0


async def rtk_updated_input(tool_input: dict[str, Any]) -> dict[str, Any] | None:
    """The Bash tool input rewritten by ``rtk hook claude``, or None when unchanged."""
    payload = json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": tool_input}
    ).encode()
    try:
        process = await asyncio.create_subprocess_exec(
            "rtk",
            "hook",
            "claude",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.debug("rtk unavailable; running Bash commands unmodified: %s", exc)
        return None
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(payload), RTK_HOOK_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning(
            "rtk hook timed out after %.1fs; passing command through", RTK_HOOK_TIMEOUT_SECONDS
        )
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        return None
    if process.returncode != 0 or not stdout.strip():
        return None
    try:
        parsed = json.loads(stdout)
    except ValueError:
        logger.debug("rtk hook returned non-JSON output; passing command through")
        return None
    if not isinstance(parsed, dict):
        return None
    specific = parsed.get("hookSpecificOutput")
    updated = specific.get("updatedInput") if isinstance(specific, dict) else None
    return updated if isinstance(updated, dict) else None


def build_pre_tool_use_hook(*, yolo: bool) -> Callable[..., Awaitable[dict[str, Any]]]:
    """SDK ``PreToolUse`` hook: RTK-rewrite Bash and, unless yolo, force the broker ask."""

    async def _pre_tool_use(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: Any,
    ) -> dict[str, Any]:
        del tool_use_id, context
        output: dict[str, Any] = {"hookEventName": "PreToolUse"}
        if not yolo:
            # Keep tool execution on the can_use_tool → answer_interaction path.
            output["permissionDecision"] = "ask"
        if input_data.get("tool_name") == "Bash":
            tool_input = input_data.get("tool_input")
            if isinstance(tool_input, dict):
                updated = await rtk_updated_input(tool_input)
                if updated is not None:
                    output["updatedInput"] = updated
        return {"hookSpecificOutput": output}

    return _pre_tool_use
