"""Checks intercepted shell requests. This is not an OS execution boundary."""

from __future__ import annotations

import os
import shlex
from pathlib import PurePosixPath
from typing import Any, cast

import bashlex  # pyright: ignore[reportMissingTypeStubs]
from bashlex.errors import ParsingError  # pyright: ignore[reportMissingTypeStubs]
from tth_types.sandbox import CommandCheck, CommandDecision, CommandRule

_parser: Any = bashlex

_DENIED_PROGRAMS = frozenset(
    {"sudo", "su", "doas", "mount", "umount", "nsenter", "unshare", "chroot"}
)
_PUBLISH = (
    ("git", "push"),
    ("git", "send-pack"),
    ("gh", "pr", "create"),
    ("gh", "pr", "merge"),
    ("gh", "release", "create"),
    ("glab", "mr", "create"),
    ("glab", "mr", "merge"),
    ("npm", "publish"),
    ("uv", "publish"),
    ("twine", "upload"),
)


def _argv(words: list[str]) -> list[str]:
    """Remove literal wrappers without evaluating any shell code."""
    words = list(words)
    while words:
        words[0] = PurePosixPath(words[0]).name
        if words[0] in {"command", "exec", "builtin", "nohup", "rtk"}:
            words.pop(0)
            if words and words[0] == "--":
                words.pop(0)
        elif words[0] == "env":
            words.pop(0)
            while words and ("=" in words[0] or words[0] in {"--", "-i", "--ignore-environment"}):
                words.pop(0)
        else:
            break
    if words and words[0] == "git":
        index = 1
        while index < len(words) and words[index].startswith("-"):
            option = words[index]
            index += 2 if option in {"-C", "-c", "--git-dir", "--work-tree", "--namespace"} else 1
        words = ["git", *words[index:]]
    return words


def _reason(words: list[str], cwd: str, rules: tuple[CommandRule, ...]) -> str | None:
    argv = _argv(words)
    if not argv:
        return None
    if argv[0] in _DENIED_PROGRAMS:
        return "Privilege and mount commands are blocked."
    if any(tuple(argv[: len(prefix)]) == prefix for prefix in _PUBLISH):
        return "Publication must run through Agentbahn's host publication step."
    if argv[:2] == ["git", "reset"] and "--hard" in argv[2:]:
        return "git reset --hard is blocked."
    if argv[:2] == ["git", "clean"] and any(
        arg == "--force" or (arg.startswith("-") and not arg.startswith("--") and "f" in arg)
        for arg in argv[2:]
    ):
        return "Forced git clean is blocked."
    if argv[0] == "rm" and any(
        arg == "--recursive"
        or (arg.startswith("-") and not arg.startswith("--") and any(flag in arg for flag in "rR"))
        for arg in argv[1:]
    ):
        for arg in argv[1:]:
            if arg.startswith("-"):
                continue
            arg = arg.replace("${HOME}", "/home/agent").replace("$HOME", "/home/agent")
            if arg == "~" or arg.startswith("~/"):
                arg = "/home/agent" + arg[1:]
            target = os.path.normpath(arg if arg.startswith("/") else f"{cwd}/{arg}")
            if target.rstrip("/*") in {"", "/home", "/home/agent"}:
                return "Recursive deletion of root or home is blocked."
    if any(tuple(argv[: len(rule.argv)]) == rule.argv for rule in rules):
        return "This command is blocked by the project's command rules."
    if argv[0] in {"sh", "bash", "dash", "zsh"}:
        for index, arg in enumerate(argv[1:], start=1):
            if arg.startswith("-") and "c" in arg and index + 1 < len(argv):
                return check_command(CommandCheck(command=argv[index + 1], cwd=cwd), rules).reason
    return None


def check_command(request: CommandCheck, rules: tuple[CommandRule, ...] = ()) -> CommandDecision:
    def walk(node: Any) -> str | None:
        if node.kind == "command":
            words = [str(part.word) for part in node.parts if part.kind == "word"]
            if reason := _reason(words, request.cwd, rules):
                return reason
        # Includes command substitutions, pipelines, lists, and subshells.
        for value in cast(dict[str, Any], vars(node)).values():
            children: list[Any] = cast(list[Any], value) if isinstance(value, list) else [value]
            for child in children:
                if hasattr(child, "kind") and (reason := walk(child)):
                    return reason
        return None

    try:
        nodes = cast(list[Any], _parser.parse(request.command))
        reason = next((reason for node in nodes if (reason := walk(node))), None)
    except (ValueError, NotImplementedError, ParsingError):
        reason = "The command could not be parsed for the project command guard."
    return CommandDecision(allowed=reason is None, reason=reason)


def check_argv(
    argv: tuple[str, ...], cwd: str, rules: tuple[CommandRule, ...] = ()
) -> CommandDecision:
    return check_command(CommandCheck(command=shlex.join(argv), cwd=cwd), rules)
