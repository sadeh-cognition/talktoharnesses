"""Validate the generated Obsidian wiki structure and links."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from talktoharnesses.wiki_lint import lint_wiki


def _default_wiki_root() -> Path:
    return Path(os.environ.get("LLM_WIKI_ROOT", "llm-wiki"))


class Command(BaseCommand):
    help = "Validate the generated Obsidian wiki structure and links."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--root",
            type=Path,
            help="Obsidian vault root; defaults to LLM_WIKI_ROOT or ./llm-wiki.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        root = options.get("root") or _default_wiki_root()
        if not root.is_dir():
            raise CommandError(f"wiki root is not a directory: {root}")

        self.stdout.write(f"[wiki_lint] using wiki root: {root}")
        issues = lint_wiki(root)
        if issues:
            for issue in issues:
                self.stdout.write(self.style.ERROR(str(issue)))
            raise CommandError(f"wiki lint failed with {len(issues)} issue(s)")
        self.stdout.write(self.style.SUCCESS("Wiki lint passed."))
