"""RTK (https://github.com/rtk-ai/rtk) configuration seeded into sandbox homes.

RTK rewrites shell commands to ``rtk <cmd>`` so the harness sees trimmed
output. The binary ships in the split images; the per-kind hook, plugin, or
rules file must live under the agent's home, which is a managed volume, so it
is written by ``rtk init`` in a one-shot container at container preparation
(the same shape as credential seeding). Claude wires RTK as an in-process SDK
hook instead, so it needs no seeded file; grok, muse, and prime_agent images
carry no rtk at all. Seeding fails open: a broken or missing ``rtk`` must
never make a sandbox unavailable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from tth_types.enums import HarnessKind

from talktoharnesses.remote import docker_ops

logger = logging.getLogger(__name__)


# ``rtk init --codex`` writes ``~/.codex/RTK.md`` and references it from
# ``~/.codex/AGENTS.md`` as ``@<path>``, but Codex does not expand such
# references in its global instructions (verified against openai-codex 0.144:
# the model reports no rtk guidance). Inline the rules in place of the
# reference; a no-op once inlined, and other AGENTS.md content is preserved.
_CODEX_INLINE_RTK_RULES = (
    "codex = Path.home() / '.codex'; agents = codex / 'AGENTS.md'; rules = codex / 'RTK.md'; "
    "text = agents.read_text(); reference = f'@{rules}'; "
    "agents.write_text(text.replace(reference, rules.read_text().rstrip() + chr(10))) "
    "if reference in text else None"
)


@dataclass(frozen=True)
class RtkInitSpec:
    """How ``rtk init --global`` seeds one kind's home."""

    init_args: tuple[str, ...]
    # Created before ``rtk init``: rtk 0.49 writes its files atomically via a
    # temp file in the target directory and does not create missing parents.
    home_directories: tuple[str, ...]
    # Python statements run after a successful ``rtk init``.
    post_init_python: str = ""


RTK_INIT_SPECS: dict[HarnessKind, RtkInitSpec] = {
    # ``--codex`` rejects ``--auto-patch`` (nothing to patch: rules file only).
    HarnessKind.CODEX: RtkInitSpec(("--codex",), (".codex",), _CODEX_INLINE_RTK_RULES),
    # Cursor init also writes the Claude Code files under ~/.claude.
    HarnessKind.CURSOR: RtkInitSpec(("--agent", "cursor", "--auto-patch"), (".claude", ".cursor")),
    HarnessKind.OPENCODE: RtkInitSpec(
        ("--opencode", "--auto-patch"), (".config/opencode/plugins",)
    ),
}


def rtk_init_command(kind: HarnessKind) -> list[str] | None:
    """The one-shot container argv seeding ``kind``'s home, or None when not seeded."""
    spec = RTK_INIT_SPECS.get(kind)
    if spec is None:
        return None
    statements = [
        "from pathlib import Path",
        "import subprocess",
        *(
            f"(Path.home() / {directory!r}).mkdir(parents=True, exist_ok=True)"
            for directory in spec.home_directories
        ),
        f"subprocess.run({['rtk', 'init', '--global', *spec.init_args]!r}, check=True)",
    ]
    if spec.post_init_python:
        statements.append(spec.post_init_python)
    return ["python", "-c", "; ".join(statements)]


def seed_rtk_config(
    client: Any,
    mount_type: Any,
    *,
    kind: HarnessKind,
    image: str,
    home_volume: str,
) -> bool:
    """Run ``rtk init`` against the sandbox home volume via a one-shot container.

    Returns True when the seeding container ran to completion. Docker or
    ``rtk`` failures are logged and swallowed so the sandbox still prepares
    with commands passing through unmodified.
    """
    command = rtk_init_command(kind)
    if command is None:
        return False
    from docker.errors import DockerException

    try:
        docker_ops.run_home_seeder(
            client, mount_type, image=image, home_volume=home_volume, command=command
        )
    except DockerException as exc:
        logger.warning(
            "rtk seeding for %s failed; commands will run without rtk: %s", kind.value, exc
        )
        return False
    return True
