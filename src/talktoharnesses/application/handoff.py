"""Canonical retained handoff representation and deterministic renderer.

One immutable handoff document type and one renderer serve both durable
harness switching and post-retention session rotation (``docs/phase8.md``
Work Package 2). Entries carry only already-retained canonical fields —
reasoning, plans, raw/native events, full tool output, and deleted rows are
never represented here; callers must exclude them before construction.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from talktoharnesses.domain._base import FROZEN
from talktoharnesses.domain.enums import MessageRole, ToolOutcome


class HandoffMessage(BaseModel):
    """Retained user/assistant message text, role, and interrupted state."""

    model_config = FROZEN

    entry_kind: Literal["message"] = "message"
    id: UUID
    turn_id: UUID
    role: MessageRole
    text: str = ""
    interrupted: bool = False
    turn_order_index: int
    order_index: int


class HandoffTool(BaseModel):
    """Canonical tool name, normalized arguments, outcome, and output tail."""

    model_config = FROZEN

    entry_kind: Literal["tool"] = "tool"
    id: UUID
    turn_id: UUID
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    outcome: ToolOutcome = ToolOutcome.UNKNOWN
    exit_status: int | None = None
    paths: tuple[str, ...] = ()
    output_tail: str = ""
    turn_order_index: int
    order_index: int


HandoffEntry = Annotated[HandoffMessage | HandoffTool, Field(discriminator="entry_kind")]


class HandoffDocument(BaseModel):
    """Ordered retained handoff for one conversation/binding."""

    model_config = FROZEN

    entries: tuple[HandoffEntry, ...] = ()


def handoff_sort_key(entry: HandoffMessage | HandoffTool) -> tuple[int, int, str]:
    """Canonical merge order shared by the persistence reader and the renderer."""
    return (entry.turn_order_index, entry.order_index, str(entry.id))


def _render_message(message: HandoffMessage) -> str:
    suffix = " (interrupted)" if message.interrupted else ""
    return f"[{message.role.value}]{suffix}: {message.text}"


def _render_tool(tool: HandoffTool) -> str:
    segments = [f"[tool:{tool.tool_name}]", f"outcome={tool.outcome.value}"]
    if tool.exit_status is not None:
        segments.append(f"exit_status={tool.exit_status}")
    if tool.arguments:
        segments.append(f"arguments={json.dumps(tool.arguments, sort_keys=True, default=str)}")
    if tool.paths:
        segments.append(f"paths={','.join(tool.paths)}")
    if tool.output_tail:
        segments.append(f"output={tool.output_tail}")
    return " ".join(segments)


def render_handoff(doc: HandoffDocument) -> str:
    """Render one deterministic text prompt from typed retained entries only.

    Entries are ordered by ``(turn_order_index, order_index, id)`` so the same
    document always renders identically regardless of read order.
    """
    ordered = sorted(doc.entries, key=handoff_sort_key)
    lines: list[str] = []
    for entry in ordered:
        if isinstance(entry, HandoffMessage):
            lines.append(_render_message(entry))
        else:
            lines.append(_render_tool(entry))
    return "\n".join(lines)
