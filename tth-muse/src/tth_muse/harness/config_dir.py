"""Per-process Muse configuration directories that carry a harness's MCP servers.

Muse Code declares MCP servers in ``$XDG_CONFIG_HOME/muse/settings.json`` and
offers no per-session path over MSP, so a harness whose configuration names
servers gets a private config directory: the host's saved settings with the
servers merged in, plus links to the host's credential and trust files. The
directory lives for one ``muse serve`` process and is removed on close.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, cast

from tth_types.harness import HarnessConfiguration, HarnessMcpServer

SETTINGS_FILE = "settings.json"
_LINKED_FILES = ("auth.json", "trust.json")
_PREFIX = "tth-muse-config-"


def base_config_dir(environ: dict[str, str] | None = None) -> Path:
    """The Muse config directory the host would use without an override."""
    env = os.environ if environ is None else environ
    xdg = env.get("XDG_CONFIG_HOME")
    root = Path(xdg) if xdg else Path(env.get("HOME", Path.home())) / ".config"
    return root / "muse"


def mcp_server_settings(server: HarnessMcpServer) -> dict[str, Any]:
    """One ``mcp_servers`` entry in Muse's streamable HTTP shape."""
    entry: dict[str, Any] = {"transport": "streamable_http", "url": server.url}
    if server.headers:
        entry["headers"] = {header.name: header.value for header in server.headers}
    return entry


def settings_with_mcp_servers(
    base_settings: dict[str, Any], config: HarnessConfiguration
) -> dict[str, Any]:
    """Merge the configuration's servers over ``base_settings``.

    Servers named in the harness replace same-named entries; other saved
    servers stay so the host keeps whatever the operator already enabled.
    """
    settings = dict(base_settings)
    settings.setdefault("schema_version", 1)
    existing = settings.get("mcp_servers")
    servers: dict[str, Any] = (
        dict(cast(dict[str, Any], existing)) if isinstance(existing, dict) else {}
    )
    for server in config.mcp_servers:
        servers[server.name] = mcp_server_settings(server)
    settings["mcp_servers"] = servers
    return settings


def _read_settings(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return cast(dict[str, Any], loaded) if isinstance(loaded, dict) else {}


def render_config_dir(
    config: HarnessConfiguration,
    *,
    base: Path | None = None,
    root: Path | None = None,
) -> Path:
    """Write a config root for one host and return the ``XDG_CONFIG_HOME`` value.

    ``base`` defaults to the host's ordinary Muse config directory; ``root``
    is where the private directory is created (system temp by default).
    """
    source = base if base is not None else base_config_dir()
    xdg_root = Path(tempfile.mkdtemp(prefix=_PREFIX, dir=root))
    muse_dir = xdg_root / "muse"
    muse_dir.mkdir(mode=0o700)
    settings = settings_with_mcp_servers(_read_settings(source / SETTINGS_FILE), config)
    settings_path = muse_dir / SETTINGS_FILE
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    settings_path.chmod(0o600)
    for name in _LINKED_FILES:
        target = source / name
        if target.is_file():
            # A link keeps in-place credential refreshes visible to the host's
            # own configuration; the settings file is the only private copy.
            (muse_dir / name).symlink_to(target)
    return xdg_root


def remove_config_dir(xdg_root: Path) -> None:
    """Delete a directory made by :func:`render_config_dir`; never follow links."""
    if xdg_root.name.startswith(_PREFIX):
        shutil.rmtree(xdg_root, ignore_errors=True)
