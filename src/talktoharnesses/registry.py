"""Name → driver factory registry."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from talktoharnesses.adapter import Harness
from talktoharnesses.errors import UnknownHarnessError

# A driver factory builds a Harness from cwd + config kwargs.
DriverFactory = Callable[..., Harness]


class HarnessConfig(BaseModel):
    """Base config shared by every driver; drivers subclass for extras."""

    model_config = ConfigDict(extra="allow")

    cwd: Path = Field(default_factory=lambda: Path.cwd())
    model: str | None = None
    binary: str | None = None
    """Override path/name of the provider CLI binary."""
    env: dict[str, str] = Field(default_factory=dict)
    """Extra environment variables for the spawned process."""


_REGISTRY: dict[str, DriverFactory] = {}


def register(name: str, factory: DriverFactory) -> None:
    """Register a driver factory under ``name`` (overwrites if present)."""
    _REGISTRY[name] = factory


def unregister(name: str) -> None:
    """Remove a registration (used in tests)."""
    _REGISTRY.pop(name, None)


def registered_names() -> list[str]:
    """Return sorted registered harness names."""
    return sorted(_REGISTRY)


def get_factory(name: str) -> DriverFactory:
    """Look up a driver factory or raise ``UnknownHarnessError``."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownHarnessError(name, registered_names()) from None


def create_harness(name: str, **kwargs: Any) -> Harness:
    """Instantiate a harness by name."""
    factory = get_factory(name)
    return factory(**kwargs)


_drivers_loaded = False


def _ensure_builtin_drivers() -> None:
    """Lazily import built-in drivers so optional deps only load when needed.

    Safe to call repeatedly.
    """
    global _drivers_loaded
    if _drivers_loaded:
        return
    _drivers_loaded = True

    # Import side-effect free factories; each module is optional-dep safe.
    try:
        from talktoharnesses.drivers.codex import create_codex_harness

        register("codex", create_codex_harness)
    except ImportError:
        pass

    try:
        from talktoharnesses.drivers.cursor import create_cursor_harness

        register("cursor", create_cursor_harness)
    except ImportError:
        pass

    try:
        from talktoharnesses.drivers.grok import create_grok_harness

        register("grok", create_grok_harness)
    except ImportError:
        pass

    try:
        from talktoharnesses.drivers.claude import create_claude_harness

        register("claude", create_claude_harness)
    except ImportError:
        pass

    try:
        from talktoharnesses.drivers.opencode import create_opencode_harness

        register("opencode", create_opencode_harness)
    except ImportError:
        pass


def ensure_drivers_loaded() -> None:
    """Public entry used by the factory before lookup."""
    _ensure_builtin_drivers()


# ---------------------------------------------------------------------------
# Built-in names (documented even before drivers land)
# ---------------------------------------------------------------------------

KNOWN_HARNESS_NAMES: tuple[str, ...] = (
    "claude",
    "codex",
    "cursor",
    "grok",
    "opencode",
)


def is_known_name(name: str) -> bool:
    """Whether ``name`` is one of the five planned harnesses (not necessarily registered)."""
    return name in KNOWN_HARNESS_NAMES


def register_many(mapping: Mapping[str, DriverFactory]) -> None:
    """Bulk registration helper for tests and plugins."""
    for name, factory in mapping.items():
        register(name, factory)
