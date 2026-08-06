"""Guard the Codex driver against upstream protocol drift.

The vendored schemas under ``codex/_generated/schemas`` are only worth carrying
if something checks them. These tests fail when a hand-written method name or
item-type mapping stops matching the schema the models were generated from, so
a Codex release that renames a notification is caught here rather than as
silently missing events at runtime.

Regenerate with ``python scripts/generate_codex_models.py`` and update the
constants when one of these fails.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from talktoharnesses.codex import methods
from talktoharnesses.drivers.codex import _ITEM_TYPES

SCHEMA_DIR = Path(methods.__file__).parent / "_generated" / "schemas"
BUNDLE = SCHEMA_DIR / "codex_app_server_protocol.v2.schemas.json"
#: Method names live on the per-message envelopes, not in the v2 type bundle.
METHOD_SCHEMAS = (
    "ClientRequest.json",
    "ClientNotification.json",
    "ServerRequest.json",
    "ServerNotification.json",
)


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _literal_values(node: Any, key: str) -> set[str]:
    """Collect string literals declared for ``key`` as ``const`` or ``enum``."""
    found: set[str] = set()
    if isinstance(node, dict):
        target = node.get(key)
        if isinstance(target, dict):
            if isinstance(target.get("const"), str):
                found.add(target["const"])
            enum = target.get("enum")
            if isinstance(enum, list):
                found |= {v for v in enum if isinstance(v, str)}
        for value in node.values():
            found |= _literal_values(value, key)
    elif isinstance(node, list):
        for item in node:
            found |= _literal_values(item, key)
    return found


@pytest.fixture(scope="module")
def schema_methods() -> set[str]:
    found: set[str] = set()
    for name in METHOD_SCHEMAS:
        path = SCHEMA_DIR / name
        assert path.is_file(), f"missing vendored schema: {path}"
        found |= _literal_values(_load(path), "method")
    assert found, "no method names found in the vendored schemas"
    return found


def test_schema_bundle_is_vendored() -> None:
    assert BUNDLE.is_file()
    assert BUNDLE.stat().st_size > 0
    ref = (SCHEMA_DIR / "UPSTREAM_REF").read_text(encoding="utf-8").strip()
    assert ref, "UPSTREAM_REF must pin the openai/codex commit models came from"


def test_generated_models_are_checked_in() -> None:
    """The driver's protocol types must exist, not just the schemas."""
    from talktoharnesses.codex._generated import models

    assert len([n for n in dir(models) if n[0].isupper()]) > 100


@pytest.mark.parametrize(
    "constant",
    sorted(
        {
            *methods.ALL_CLIENT_METHODS,
            *methods.ALL_NOTIFICATIONS,
            *methods.ALL_SERVER_REQUESTS,
            methods.ClientNotifications.INITIALIZED,
        }
    ),
)
def test_method_constant_exists_upstream(constant: str, schema_methods: set[str]) -> None:
    assert constant in schema_methods, (
        f"{constant!r} is not a method in the vendored Codex schema — upstream "
        "renamed or removed it; regenerate and update codex/methods.py"
    )


def test_item_type_mapping_matches_schema() -> None:
    """Every Codex item type we map must still be a real item type upstream."""
    item_types = _literal_values(_load(BUNDLE), "type")
    unknown = sorted(k for k in _ITEM_TYPES if k not in item_types)
    assert not unknown, (
        f"item types no longer present upstream: {unknown} — "
        "these map to 'unknown' at runtime and silently lose events"
    )
