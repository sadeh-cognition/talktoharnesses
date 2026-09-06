#!/usr/bin/env python3
"""Fail when a module vendored into the tth-<kind> splits has drifted between them.

The split-services decision keeps non-schema shared code (ACP transport, process
supervisor, path checks, the split HTTP surface) as a copy inside every split that
needs it. Copies are only safe while they stay identical, so this check normalizes
the per-split tokens (package name, service name, ``HarnessKind`` member, executable
env var) and diffs every vendored file across the splits that carry it.

Usage: ``python scripts/check_split_drift.py [--verbose]``. Exit status 1 on drift.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Split:
    kind: str  # directory suffix, e.g. "prime-agent"
    executable: str | None  # CLI name resolved from PATH, None for SDK-managed kinds

    @property
    def package(self) -> str:
        return "tth_" + self.kind.replace("-", "_")

    @property
    def upper(self) -> str:
        return self.kind.replace("-", "_").upper()

    @property
    def root(self) -> Path:
        return ROOT / f"tth-{self.kind}"

    @property
    def group(self) -> str:
        return "sdk" if self.executable is None else "supervised"


SPLITS: tuple[Split, ...] = (
    Split("grok", "grok"),
    Split("cursor", "cursor-agent"),
    Split("opencode", "opencode"),
    Split("prime-agent", "prime-agent"),
    Split("muse", "muse"),
    Split("claude", None),
    Split("codex", None),
)

# Files that must be identical (after normalization) across every split that has
# them. ``grouped`` files are allowed to differ between the supervised and the
# SDK-managed splits, but must match within each group.
VENDORED_SOURCE: dict[str, bool] = {
    "acp/connection.py": False,
    "acp/framing.py": False,
    "acp/jsonrpc.py": False,
    "acp/pending.py": False,
    "acp/schemas/__init__.py": False,
    "acp/schemas/base.py": False,
    "api.py": True,
    "asgi.py": False,
    "auth.py": False,
    "errors.py": False,
    "runtime/__init__.py": False,
    "runtime/handle.py": False,
    "runtime/process_bound.py": False,
    "runtime/spec.py": False,
    "runtime/supervisor.py": False,
    "runtime/windows_job.py": False,
    "sessions.py": True,
    "settings.py": False,
    "shared/__init__.py": False,
    "shared/compatibility.py": False,
    "shared/effort.py": False,
    "shared/model_discovery.py": False,
    "shared/paths.py": True,
    "shared/policy.py": False,
    "shared/questions.py": False,
    "shared/redaction.py": False,
    "shared/sse_decoder.py": False,
    "sse.py": False,
    "telemetry.py": False,
    "testing.py": True,
    "urls.py": False,
}

VENDORED_TESTS: dict[str, bool] = {
    "acp/test_framing.py": False,
    "acp/test_jsonrpc.py": False,
    "test_effort.py": False,
    "test_process_bound.py": False,
    "test_telemetry.py": False,
}


# Files whose copies name their own kind (``HarnessKind.GROK``, the executable env
# var, the CLI name). Kind tokens are normalized only here, so a multi-kind branch
# in a genuinely shared module still has to match byte for byte.
KIND_PARAMETERIZED: frozenset[str] = frozenset(
    {
        "shared/paths.py",
        "testing.py",
        "test_process_bound.py",
        "test_telemetry.py",
    }
)


def normalize(text: str, split: Split, relpath: str) -> str:
    text = text.replace(split.package, "tth_SPLIT")
    text = text.replace(f"tth-{split.kind}", "tth-SPLIT")
    if relpath not in KIND_PARAMETERIZED:
        return text
    text = re.sub(rf"\bHarnessKind\.{split.upper}\b", "HarnessKind.SPLIT", text)
    text = text.replace(
        f"TALKTOHARNESSES_{split.upper}_EXECUTABLE", "TALKTOHARNESSES_SPLIT_EXECUTABLE"
    )
    if split.executable is not None:
        text = text.replace(f'"{split.executable}"', '"SPLIT_EXECUTABLE"')
    return text


def check(files: dict[str, bool], subdir: str, *, verbose: bool) -> list[str]:
    problems: list[str] = []
    for relpath, grouped in sorted(files.items()):
        copies: dict[str, list[tuple[Split, str]]] = {}
        for split in SPLITS:
            base = split.root / subdir
            if subdir == "src":
                base = base / split.package
            path = base / relpath
            if not path.exists():
                continue
            key = split.group if grouped else "all"
            copies.setdefault(key, []).append((split, normalize(path.read_text(), split, relpath)))
        for key, members in copies.items():
            if len(members) < 2:
                continue
            reference, reference_text = members[0]
            for split, text in members[1:]:
                if text == reference_text:
                    continue
                label = f"{subdir}/{relpath}" + (f" [{key}]" if grouped else "")
                problems.append(f"{label}: tth-{reference.kind} != tth-{split.kind}")
                if verbose:
                    diff = difflib.unified_diff(
                        reference_text.splitlines(),
                        text.splitlines(),
                        fromfile=f"tth-{reference.kind}/{subdir}/{relpath}",
                        tofile=f"tth-{split.kind}/{subdir}/{relpath}",
                        lineterm="",
                    )
                    problems.extend("    " + line for line in diff)
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verbose", action="store_true", help="print unified diffs")
    args = parser.parse_args()
    problems = check(VENDORED_SOURCE, "src", verbose=args.verbose)
    problems += check(VENDORED_TESTS, "tests", verbose=args.verbose)
    if problems:
        print("vendored split modules have drifted:")
        print("\n".join(problems))
        print("\nRe-sync the copies (or update VENDORED_* in scripts/check_split_drift.py).")
        return 1
    print("split copies are in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
