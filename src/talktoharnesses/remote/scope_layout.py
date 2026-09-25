"""Names, lock and teardown of everything one policy sandbox scope owns.

:class:`~talktoharnesses.remote.isolated_sandbox.IsolatedSandbox` creates a
scope's resources and :meth:`ScopedSandboxManager.reclaim` removes them; both
go through :class:`ScopeLayout`, so the naming scheme lives in one place.
"""

from __future__ import annotations

import contextlib
import fcntl
import re
import shutil
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from talktoharnesses.remote.mcp_relay import stop_mcp_relay

SCOPE_PREFIX = "tth-scope-"
# Every Docker resource and state directory of a scope is named from this.
SCOPE_NAME = re.compile(SCOPE_PREFIX + "[0-9a-f]{24}")
# Docker label naming the scope that owns a container or network.
SCOPE_LABEL = "tth.sandbox"
# Locks live beside the state directories, not inside them: purging a scope
# removes its directory, which must never take a held lock with it.
LOCKS_DIR = ".locks"


@dataclass(frozen=True)
class ScopeLayout:
    """Where one scope's containers, network, volumes and state live."""

    name: str
    state_root: Path

    @property
    def gateway(self) -> str:
        return self.name + "-gateway"

    @property
    def network(self) -> str:
        return self.name + "-network"

    @property
    def home_volume(self) -> str:
        return self.name + "-home"

    @property
    def data_volume(self) -> str:
        return self.name + "-data"

    @property
    def state(self) -> Path:
        return self.state_root / self.name

    @property
    def config_file(self) -> Path:
        """The gateway configuration, rewritten on every preparation."""
        return self.state / "config.json"

    @property
    def mounts_file(self) -> Path:
        return self.state / "mounts.json"

    @property
    def last_used_file(self) -> Path:
        """Its mtime is the scope's last use, shared by every proxy process on the host."""
        return self.state / "last-used"

    @contextlib.contextmanager
    def locked(self) -> Generator[None]:
        """Hold the scope's cross-process lock; preparing and reclaiming take it."""
        locks = self.state_root / LOCKS_DIR
        locks.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (locks / (self.name + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def touch(self) -> None:
        """Record a use of the scope; a no-op while it has no state directory."""
        with contextlib.suppress(FileNotFoundError):
            self.last_used_file.touch()

    def remove(self, client: Any, *, purge: bool) -> None:
        """Blocking; remove the scope's MCP relay, containers and network.

        With ``purge``, also remove its session volumes and state directory.
        Resources that are already gone are skipped.
        """
        with self.locked():
            stop_mcp_relay(self.state)
            _remove(client.containers, self.name, force=True)
            _remove(client.containers, self.gateway, force=True)
            _remove(client.networks, self.network)
            if not purge:
                return
            _remove(client.volumes, self.home_volume, force=True)
            _remove(client.volumes, self.data_volume, force=True)
            if self.state.is_dir():
                shutil.rmtree(self.state)


def _remove(collection: Any, name: str, **options: object) -> None:
    from docker.errors import NotFound

    with contextlib.suppress(NotFound):
        collection.get(name).remove(**options)
