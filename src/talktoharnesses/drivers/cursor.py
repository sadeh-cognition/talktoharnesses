"""Cursor harness — ``cursor-agent acp`` over ACP stdio."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from talktoharnesses.acp.runtime import AcpRuntime, AcpSpawnInput
from talktoharnesses.types import Capabilities


class CursorHarness(AcpRuntime):
    name = "cursor"
    capabilities = Capabilities(
        session_model_switch="unsupported",
        interrupt_turn="in-session",
        approval="in-session",
        user_input="in-session",
        resume_session="in-session",
    )

    def __init__(
        self,
        *,
        cwd: Path | str = ".",
        model: str | None = None,
        binary: str | None = None,
        env: Mapping[str, str] | None = None,
        endpoint: str | None = None,
        command: Sequence[str] | None = None,
        **_ignored: Any,
    ) -> None:
        path = Path(cwd).resolve()
        cmd = list(command) if command is not None else [binary or "cursor-agent"]
        if command is None:
            if endpoint:
                cmd.extend(["-e", endpoint])
            cmd.append("acp")
        spawn = AcpSpawnInput(
            command=cmd,
            env=dict(env or {}),
            cwd=path,
            provider="cursor",
            auth_method_id="cursor_login",
        )
        super().__init__(spawn, model=model)


def create_cursor_harness(**kwargs: Any) -> CursorHarness:
    return CursorHarness(**kwargs)
