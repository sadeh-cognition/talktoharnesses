#!/usr/bin/env python3
"""Vendor Codex app-server JSON schemas and regenerate Pydantic models.

Mirrors T3's ``packages/effect-codex-app-server/scripts/generate.ts`` shape:

1. Pin ``UPSTREAM_REF`` to a commit of ``openai/codex``.
2. Fetch JSON schemas under ``codex-rs/app-server-protocol/schema/json``.
3. Vendor them under ``src/talktoharnesses/codex/_generated/schemas/``.
4. Run ``datamodel-code-generator`` into ``_generated/models.py``.

Usage::

    python scripts/generate_codex_models.py
    python scripts/generate_codex_models.py --ref <sha>
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = "openai/codex"
SCHEMA_DIR = "codex-rs/app-server-protocol/schema/json"
# Pin by default; override with --ref.
UPSTREAM_REF = "57f42a81131c"

ROOT = Path(__file__).resolve().parents[1]
OUT_SCHEMAS = ROOT / "src" / "talktoharnesses" / "codex" / "_generated" / "schemas"
OUT_MODELS = ROOT / "src" / "talktoharnesses" / "codex" / "_generated" / "models.py"
API = "https://api.github.com/repos/{repo}/contents/{path}?ref={ref}"
RAW = "https://raw.githubusercontent.com/{repo}/{ref}/{path}"


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "talktoharnesses-codegen"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 — fixed upstream URL
        return resp.read()


def list_schema_files(ref: str) -> list[str]:
    url = API.format(repo=REPO, path=SCHEMA_DIR, ref=ref)
    data = json.loads(_http_get(url).decode())
    names: list[str] = []
    for entry in data:
        if entry.get("type") == "file" and entry["name"].endswith(".json"):
            names.append(entry["name"])
    return sorted(names)


def vendor_schemas(ref: str) -> list[Path]:
    OUT_SCHEMAS.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in list_schema_files(ref):
        raw_url = RAW.format(repo=REPO, ref=ref, path=f"{SCHEMA_DIR}/{name}")
        target = OUT_SCHEMAS / name
        target.write_bytes(_http_get(raw_url))
        written.append(target)
        print(f"vendored {name}")
    # Write pin marker
    (OUT_SCHEMAS / "UPSTREAM_REF").write_text(ref + "\n", encoding="utf-8")
    return written


def generate_models() -> None:
    # Prefer the combined v2 schema if present; else first schema file.
    combined = OUT_SCHEMAS / "codex_app_server_protocol.v2.schemas.json"
    if not combined.exists():
        combined = OUT_SCHEMAS / "codex_app_server_protocol.schemas.json"
    if not combined.exists():
        raise SystemExit(f"No combined schema found under {OUT_SCHEMAS}")

    cmd = [
        sys.executable,
        "-m",
        "datamodel_code_generator",
        "--input",
        str(combined),
        "--input-file-type",
        "jsonschema",
        "--output",
        str(OUT_MODELS),
        "--output-model-type",
        "pydantic_v2.BaseModel",
        "--use-standard-collections",
        "--use-union-operator",
        "--target-python-version",
        "3.11",
        "--disable-timestamp",
    ]
    print("running:", " ".join(cmd))
    subprocess.check_call(cmd)
    print(f"wrote {OUT_MODELS}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default=UPSTREAM_REF, help="openai/codex git ref")
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Only regenerate models from already-vendored schemas",
    )
    parser.add_argument(
        "--skip-generate",
        action="store_true",
        help="Only vendor schemas, do not run datamodel-code-generator",
    )
    args = parser.parse_args()

    if not args.skip_fetch:
        vendor_schemas(args.ref)
    if not args.skip_generate:
        generate_models()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
