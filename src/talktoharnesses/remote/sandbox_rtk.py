"""RTK (https://github.com/rtk-ai/rtk) configuration seeded into sandbox homes.

RTK rewrites shell commands to ``rtk <cmd>`` so the harness sees trimmed
output. The binary ships in the split images; the per-kind hook, plugin, or
rules file must live under the agent's home, which is a managed volume, so it
is written by ``rtk init`` in a one-shot container at container preparation
(the same shape as credential seeding). Claude wires RTK as an in-process SDK
hook instead, so it needs no seeded file; prime_agent carries no rtk.

Codex, Grok, and Muse have no RTK hook and follow RTK's Codex rules file from
their system prompt instead: Codex reads ``~/.codex/AGENTS.md``, Grok reads
``~/.grok/AGENTS.md`` as its global rules, and Muse loads ``~/.codex/AGENTS.md``
as compatible personal rules by default (verified against grok 1.0.34 and
muse 1.3.0). Seeding fails open: a broken or missing ``rtk`` must never make a
sandbox unavailable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from tth_types.enums import HarnessKind

from talktoharnesses.remote import docker_ops

logger = logging.getLogger(__name__)


def _adopt_codex_rules(rules_file: str) -> str:
    """Python statements carrying ``rtk init --codex``'s rules into ``rules_file``.

    ``rtk init --codex`` writes ``~/.codex/RTK.md`` and appends ``@<path>`` to
    ``~/.codex/AGENTS.md``, but Codex does not expand such references in its
    global instructions (verified against openai-codex 0.144: the model
    reports no rtk guidance), and Grok reads a different file. The rules text
    is inlined into ``rules_file`` (relative to the home) instead: the
    reference is dropped, other content is preserved, and a home that already
    carries the rules is left alone, so re-seeding every preparation neither
    duplicates nor re-references them.
    """
    return f"""\
codex_rules = Path.home() / '.codex' / 'RTK.md'
rules = codex_rules.read_text().rstrip() + '\\n'
target = Path.home() / {rules_file!r}
text = target.read_text().replace(f'@{{codex_rules}}', '').strip() if target.exists() else ''
if rules.strip() not in text:
    text = f'{{text}}\\n\\n{{rules}}' if text else rules
target.write_text(text if text.endswith('\\n') else text + '\\n')"""


@dataclass(frozen=True)
class RtkInitSpec:
    """How ``rtk init --global`` seeds one kind's home."""

    init_args: tuple[str, ...]
    # Created before ``rtk init``: rtk 0.49 writes its files atomically via a
    # temp file in the target directory and does not create missing parents.
    home_directories: tuple[str, ...]
    # Python statements run after a successful ``rtk init``.
    post_init_python: str = ""


# ``--codex`` rejects ``--auto-patch`` (nothing to patch: rules file only).
_CODEX_RULES = RtkInitSpec(("--codex",), (".codex",), _adopt_codex_rules(".codex/AGENTS.md"))

RTK_INIT_SPECS: dict[HarnessKind, RtkInitSpec] = {
    HarnessKind.CODEX: _CODEX_RULES,
    # Cursor init also writes the Claude Code files under ~/.claude.
    HarnessKind.CURSOR: RtkInitSpec(("--agent", "cursor", "--auto-patch"), (".claude", ".cursor")),
    HarnessKind.OPENCODE: RtkInitSpec(
        ("--opencode", "--auto-patch"), (".config/opencode/plugins",)
    ),
    # Grok appends ~/.grok/AGENTS.md to its system prompt; the Codex files
    # rtk init leaves under ~/.grok's sibling ~/.codex are unread there.
    HarnessKind.GROK: RtkInitSpec(
        ("--codex",), (".codex", ".grok"), _adopt_codex_rules(".grok/AGENTS.md")
    ),
    # Muse reads ~/.codex/AGENTS.md as personal rules unless
    # ``context.foreign_personal_rules`` is off.
    HarnessKind.MUSE: _CODEX_RULES,
}


def rtk_init_command(kind: HarnessKind) -> list[str] | None:
    """The one-shot container argv seeding ``kind``'s home, or None when not seeded."""
    spec = RTK_INIT_SPECS.get(kind)
    if spec is None:
        return None
    script = "\n".join(
        [
            "from pathlib import Path",
            "import subprocess",
            *(
                f"(Path.home() / {directory!r}).mkdir(parents=True, exist_ok=True)"
                for directory in spec.home_directories
            ),
            f"subprocess.run({['rtk', 'init', '--global', *spec.init_args]!r}, check=True)",
            spec.post_init_python,
        ]
    )
    return ["python", "-c", script]


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
