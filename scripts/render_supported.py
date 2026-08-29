"""Aggregate split-owned compatibility documents into SUPPORTED_HARNESSES.md."""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SPLITS = (
    ("grok", "tth-grok", "Grok"),
    ("cursor", "tth-cursor", "Cursor"),
    ("codex", "tth-codex", "Codex"),
    ("claude", "tth-claude", "Claude Code"),
    ("opencode", "tth-opencode", "OpenCode"),
    ("prime_agent", "tth-prime-agent", "Prime Agent"),
)
CAPABILITIES = (
    "supports_resume",
    "supports_interrupt",
    "supports_steer",
    "supports_multi_interaction",
    "supports_nested_activity",
)
KNOWN_PLATFORMS = frozenset({"linux", "darwin", "win32"})


def _document_path(kind: str, directory: str) -> Path:
    return ROOT / directory / "src" / f"tth_{kind}" / "data" / "compatibility" / f"{kind}.json"


def load_documents() -> list[tuple[str, str, dict[str, Any]]]:
    documents: list[tuple[str, str, dict[str, Any]]] = []
    for kind, directory, title in SPLITS:
        path = _document_path(kind, directory)
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"{path} must contain a JSON object")
        documents.append((kind, title, document))
    return documents


def validate_documents(documents: list[tuple[str, str, dict[str, Any]]], mode: str) -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_version = project["project"]["version"]
    if mode == "stable" and ".dev" in package_version.lower():
        raise ValueError("stable validation rejects a development proxy version")

    for kind, _title, document in documents:
        adapter_version = document.get("adapter_version")
        floor = document.get("floor")
        if not isinstance(adapter_version, str) or not adapter_version:
            raise ValueError(f"{kind}: adapter_version is required")
        if not isinstance(floor, dict):
            raise ValueError(f"{kind}: floor object is required")
        if not isinstance(floor.get("version"), str) or not floor["version"]:
            raise ValueError(f"{kind}: floor.version is required")
        platforms = floor.get("platforms")
        if not isinstance(platforms, list) or not platforms:
            raise ValueError(f"{kind}: floor.platforms must not be empty")
        if any(platform not in KNOWN_PLATFORMS for platform in platforms):
            raise ValueError(f"{kind}: floor contains an unsupported platform")
        capabilities = floor.get("capabilities")
        if not isinstance(capabilities, dict) or any(
            not isinstance(capabilities.get(name), bool) for name in CAPABILITIES
        ):
            raise ValueError(f"{kind}: floor.capabilities is incomplete")
        latest = document.get("latest_verified")
        if latest is not None:
            if not isinstance(latest, dict) or not isinstance(latest.get("version"), str):
                raise ValueError(f"{kind}: latest_verified.version is required")
            if latest.get("platform") not in platforms:
                raise ValueError(f"{kind}: latest_verified platform is outside the floor")
        if mode == "stable" and adapter_version != package_version:
            raise ValueError(
                f"{kind}: adapter version {adapter_version} does not match {package_version}"
            )


def _floor_label(kind: str, floor: dict[str, Any]) -> str:
    if kind == "codex":
        return (
            f"SDK `{floor['sdk_version']}` + `{floor['runtime_package']}` "
            f"`{floor['runtime_version']}` (exact)"
        )
    if kind == "claude":
        return f"SDK `{floor['sdk_version']}` + CLI `>= {floor['version']}`"
    return f"CLI `>= {floor['version']}`"


def render(documents: list[tuple[str, str, dict[str, Any]]]) -> str:
    lines = [
        "# Supported Harnesses",
        "",
        "This document is generated from compatibility data owned by the split projects.",
        "Do not edit provider tables by hand; regenerate via",
        "`uv run python scripts/render_supported.py`.",
        "",
        "Each harness publishes a **floor** (minimum identity and platforms) and",
        "adapter-owned capability flags. Models, modes, and efforts are discovered",
        "at probe from the installed CLI. Newer identities above the floor are",
        "accepted; `latest_verified` is advisory only.",
        "",
    ]
    for kind, title, document in documents:
        floor = document["floor"]
        platforms = ", ".join(floor["platforms"])
        lines.extend(
            (
                f"## {title}",
                "",
                f"- Adapter version: `{document['adapter_version']}`",
                f"- Floor: {_floor_label(kind, floor)} on {platforms}",
            )
        )
        latest = document.get("latest_verified")
        if latest is None:
            lines.append("- Latest verified: _none_")
        else:
            identity = latest.get("identity") or latest["version"]
            lines.append(f"- Latest verified: `{identity}` on {latest['platform']}")
        lines.append("- Models, modes, and efforts are discovered at probe from the installed CLI.")
        if kind in {"grok", "cursor"}:
            lines.append(f"- ACP: v{floor['acp_protocol_version']}")
        elif kind == "prime_agent":
            lines.append("- Transport: JSONL RPC")
        capabilities = floor["capabilities"]
        cells = " | ".join("yes" if capabilities[name] else "no" for name in CAPABILITIES)
        lines.extend(
            (
                "",
                "### Adapter capabilities",
                "",
                "| Resume | Interrupt | Steer | Multi-interaction | Nested |",
                "| --- | --- | --- | --- | --- |",
                f"| {cells} |",
                "",
            )
        )
        notes = floor.get("notes")
        if notes:
            lines.extend(("### Notes", "", f"- {notes}", ""))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--validate", choices=("development", "stable"), default="development")
    parser.add_argument("--output", type=Path, default=ROOT / "SUPPORTED_HARNESSES.md")
    args = parser.parse_args()

    documents = load_documents()
    validate_documents(documents, args.validate)
    content = render(documents)
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != content:
            print(f"{args.output} is out of date; run without --check to regenerate")
            return 1
        print(f"{args.output} is up to date")
        return 0
    args.output.write_text(content, encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
