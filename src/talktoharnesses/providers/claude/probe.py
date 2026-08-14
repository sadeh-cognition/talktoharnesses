"""Claude Agent SDK probe against the packaged compatibility source."""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Literal, cast

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import (
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessEffortInfo,
    HarnessModelInfo,
)
from talktoharnesses.providers.claude.compatibility import (
    ClaudeReleaseRecord,
    match_release,
)
from talktoharnesses.providers.effort import validate_effort
from talktoharnesses.runtime.paths import resolve_executable

_EFFORTS = tuple(
    HarnessEffortInfo(id=value, label=value.title())
    for value in ("low", "medium", "high", "max")
)


def _import_claude_sdk() -> Any:
    try:
        import claude_agent_sdk
    except ImportError as exc:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "claude-agent-sdk extra is not installed",
            details={"extra": "claude"},
        ) from exc
    return claude_agent_sdk


def _bundled_cli_version() -> str:
    try:
        from claude_agent_sdk._cli_version import __cli_version__

        return str(__cli_version__)
    except Exception as exc:  # noqa: BLE001
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "unable to determine bundled Claude Code CLI version",
        ) from exc


async def probe_claude(
    config: HarnessConfiguration,
) -> tuple[HarnessCapabilities, ClaudeReleaseRecord]:
    """Match installed SDK + CLI identity to a compatibility release."""
    sdk = _import_claude_sdk()
    sdk_version = str(getattr(sdk, "__version__", "") or "")
    if not sdk_version:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "claude_agent_sdk.__version__ missing",
        )
    cli_source: Literal["bundled", "explicit"] = "bundled"
    cli_path: str | None = None
    if config.executable_path:
        executable = resolve_executable(config.executable_path)
        cli_path = str(executable)
        try:
            proc = await asyncio.create_subprocess_exec(
                str(executable),
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise DomainError(
                ErrorCode.INVALID_EXECUTABLE,
                f"failed to execute Claude Code: {exc}",
                details={"executable": str(executable)},
            ) from exc
        stdout_b, stderr_b = await proc.communicate()
        if proc.returncode not in (0, None):
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "Claude Code --version failed",
                details={
                    "returncode": proc.returncode,
                    "stderr": stderr_b.decode("utf-8", errors="replace")[:500],
                },
            )
        version_output = stdout_b.decode("utf-8", errors="replace").strip()
        cli_version = version_output.removesuffix(" (Claude Code)")
        if not cli_version or "\n" in cli_version:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "malformed Claude Code version output",
                details={"version_stdout": version_output},
            )
        cli_source = "explicit"
    else:
        cli_version = _bundled_cli_version()
    release = match_release(
        sdk_version=sdk_version,
        cli_version=cli_version,
        cli_source=cli_source,
        platform=sys.platform,
    )
    models = await _discover_models(sdk, config.working_directory, cli_path)
    capabilities = release.to_harness_capabilities().model_copy(
        update={"models": models, "efforts": _EFFORTS}
    )
    validate_effort(config, capabilities)
    return capabilities, release


async def _discover_models(
    sdk: Any, working_directory: str, cli_path: str | None
) -> tuple[HarnessModelInfo, ...]:
    try:
        options = sdk.ClaudeAgentOptions(
            cwd=working_directory,
            cli_path=cli_path,
            setting_sources=[],
        )
        async with sdk.ClaudeSDKClient(options) as client:
            server_info = await client.get_server_info()
    except Exception as exc:  # noqa: BLE001
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "Claude model discovery failed",
        ) from exc
    if not isinstance(server_info, dict):
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "Claude advertised no models",
        )
    raw_models = cast(dict[object, object], server_info).get("models")
    if not isinstance(raw_models, list) or not raw_models:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "Claude advertised no models",
        )
    models: list[HarnessModelInfo] = []
    for raw_model in cast(list[object], raw_models):
        if not isinstance(raw_model, dict):
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "malformed Claude model list",
            )
        model = cast(dict[object, object], raw_model)
        model_id = model.get("value")
        label = model.get("displayName")
        if not isinstance(model_id, str) or not model_id:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "malformed Claude model list",
            )
        models.append(
            HarnessModelInfo(
                id=model_id,
                label=label if isinstance(label, str) and label else None,
            )
        )
    return tuple(models)
