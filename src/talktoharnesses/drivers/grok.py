"""Grok harness — ``grok agent stdio`` over ACP."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from talktoharnesses.acp.runtime import AcpRuntime, AcpSpawnInput
from talktoharnesses.types import Capabilities


class GrokHarness(AcpRuntime):
    name = "grok"
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
        command: Sequence[str] | None = None,
        **_ignored: Any,
    ) -> None:
        path = Path(cwd).resolve()
        cmd = (
            list(command)
            if command is not None
            else [binary or "grok", "agent", "stdio"]
        )

        merged = dict(env or {})
        # Mirror T3: tag OAuth referrer when present.
        merged.setdefault("GROK_OAUTH2_REFERRER", "talktoharnesses")

        # Prefer API key auth when available; otherwise cached token.
        has_key = bool(os.environ.get("XAI_API_KEY") or merged.get("XAI_API_KEY"))
        auth_method = "xai.api_key" if has_key else "cached_token"

        spawn = AcpSpawnInput(
            command=cmd,
            env=merged,
            cwd=path,
            provider="grok",
            auth_method_id=auth_method,
        )
        super().__init__(spawn, model=model)


def create_grok_harness(**kwargs: Any) -> GrokHarness:
    return GrokHarness(**kwargs)
