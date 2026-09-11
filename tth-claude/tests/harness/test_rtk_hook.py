"""RTK rewrite hook: shells out to ``rtk hook claude`` and fails open."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from tth_claude.harness.rtk_hook import build_pre_tool_use_hook, rtk_updated_input

REWRITE_SCRIPT = """#!/bin/sh
cat >/dev/null
printf '%s' '{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
printf '%s' '"updatedInput":{"command":"rtk git status"}}}'
"""
SILENT_SCRIPT = """#!/bin/sh
cat >/dev/null
exit 0
"""
FAILING_SCRIPT = """#!/bin/sh
echo '{"hookSpecificOutput":{"updatedInput":{"command":"ignored"}}}'
exit 1
"""
GARBAGE_SCRIPT = """#!/bin/sh
echo 'not json'
"""
SLOW_SCRIPT = """#!/bin/sh
sleep 5
"""


def _install_fake_rtk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, script: str | None) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    if script is not None:
        rtk = bin_dir / "rtk"
        rtk.write_text(script)
        rtk.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin:/bin")


@pytest.mark.asyncio
async def test_rtk_updated_input_returns_rewrite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake_rtk(monkeypatch, tmp_path, REWRITE_SCRIPT)
    assert await rtk_updated_input({"command": "git status"}) == {"command": "rtk git status"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "script",
    [None, SILENT_SCRIPT, FAILING_SCRIPT, GARBAGE_SCRIPT],
    ids=["missing", "no-rewrite", "nonzero-exit", "non-json"],
)
async def test_rtk_updated_input_fails_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, script: str | None
) -> None:
    _install_fake_rtk(monkeypatch, tmp_path, script)
    assert await rtk_updated_input({"command": "git status"}) is None


@pytest.mark.asyncio
async def test_rtk_updated_input_times_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install_fake_rtk(monkeypatch, tmp_path, SLOW_SCRIPT)
    monkeypatch.setattr("tth_claude.harness.rtk_hook.RTK_HOOK_TIMEOUT_SECONDS", 0.2)
    assert await rtk_updated_input({"command": "git status"}) is None


@pytest.mark.asyncio
async def test_pre_tool_use_hook_forces_ask_and_rewrites_bash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake_rtk(monkeypatch, tmp_path, REWRITE_SCRIPT)
    hook = build_pre_tool_use_hook(yolo=False)

    bash: dict[str, Any] = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
    assert await hook(bash, "tool-1", None) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "updatedInput": {"command": "rtk git status"},
        }
    }

    read: dict[str, Any] = {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}}
    assert await hook(read, "tool-2", None) == {
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask"}
    }


@pytest.mark.asyncio
async def test_pre_tool_use_hook_in_yolo_only_rewrites(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake_rtk(monkeypatch, tmp_path, REWRITE_SCRIPT)
    hook = build_pre_tool_use_hook(yolo=True)

    bash: dict[str, Any] = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
    assert await hook(bash, "tool-1", None) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": {"command": "rtk git status"},
        }
    }

    _install_fake_rtk(monkeypatch, tmp_path / "none", None)
    assert await hook(bash, "tool-2", None) == {
        "hookSpecificOutput": {"hookEventName": "PreToolUse"}
    }
