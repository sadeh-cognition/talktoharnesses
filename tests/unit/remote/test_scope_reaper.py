"""Reclaiming idle and dead policy sandbox scopes."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from docker.errors import APIError, ImageNotFound, NotFound
from tth_types.enums import HarnessKind
from tth_types.harness import HarnessConfiguration
from tth_types.sandbox import SandboxPolicy, SandboxPolicyRef, SandboxPolicyRevision

from talktoharnesses.remote import custom_images, scope_layout
from talktoharnesses.remote.isolated_sandbox import IsolatedSandbox
from talktoharnesses.remote.sandbox import SandboxConfig, SandboxRecordData
from talktoharnesses.remote.scope_layout import ScopeLayout
from talktoharnesses.remote.scope_reaper import ReapReport, ScopeReaper, ScopeReaperPolicy
from talktoharnesses.remote.scoped_sandboxes import ScopedSandboxManager

NOW = 2_000_000_000.0
HOUR = 60 * 60
DAY = 24 * HOUR
SCOPE = "tth-scope-" + "a" * 24
OTHER = "tth-scope-" + "b" * 24


class FakeResource:
    def __init__(
        self,
        registry: dict[str, FakeResource],
        name: str,
        *,
        status: str = "running",
        labels: dict[str, str] | None = None,
    ) -> None:
        self._registry = registry
        self.name = name
        self.status = status
        self.labels = labels or {}
        self.remove_error: Exception | None = None
        self.attrs: dict[str, Any] = {"ImageID": "sha256:" + name}
        registry[name] = self

    def remove(self, force: bool = False) -> None:
        del force
        if self.remove_error is not None:
            raise self.remove_error
        del self._registry[self.name]


class FakeCollection:
    def __init__(self) -> None:
        self.items: dict[str, FakeResource] = {}

    def list(
        self, all: bool = False, filters: dict[str, str] | None = None, sparse: bool = False
    ) -> list[FakeResource]:
        del all, sparse
        label = (filters or {}).get("label")
        return [item for item in self.items.values() if label is None or label in item.labels]

    def get(self, name: str) -> FakeResource:
        if name not in self.items:
            raise NotFound(name)
        return self.items[name]


class FakeImages:
    """No images: image cleanup runs for real and finds nothing to remove."""

    def list(self, filters: dict[str, Any]) -> list[Any]:
        del filters
        return []

    def get(self, name: str) -> Any:
        raise ImageNotFound(name)


class FakeDocker:
    def __init__(self) -> None:
        self.containers = FakeCollection()
        self.networks = FakeCollection()
        self.volumes = FakeCollection()
        self.images = FakeImages()

    def add_scope(
        self, name: str, *, main: str | None = "running", gateway: str | None = "running"
    ) -> None:
        label = {"tth.sandbox": name}
        if main is not None:
            FakeResource(self.containers.items, name, status=main, labels=label)
        if gateway is not None:
            FakeResource(self.containers.items, name + "-gateway", status=gateway, labels=label)
        FakeResource(self.networks.items, name + "-network", labels=label)
        for suffix in ("-home", "-data"):
            FakeResource(self.volumes.items, name + suffix)


class FakeStore:
    def __init__(self, *scopes: str) -> None:
        self.rows = set(scopes)
        self.deleting: asyncio.Event | None = None

    async def get(self, scope: str) -> SandboxRecordData | None:
        raise NotImplementedError

    async def upsert(self, record: SandboxRecordData) -> None:
        raise NotImplementedError

    async def reserve(self, record: SandboxRecordData) -> SandboxRecordData:
        raise NotImplementedError

    async def delete(self, scope: str) -> None:
        if self.deleting is not None:
            await self.deleting.wait()
        self.rows.discard(scope)


def _state(root: Path, name: str, *, mounted: Path, used: float) -> Path:
    state = root / name
    state.mkdir(parents=True)
    (state / "mounts.json").write_text(
        json.dumps(
            {
                "container_id": "container",
                "sources": {str(mounted): "/daemon" + str(mounted), "/etc/tth/ca.pem": "/ca"},
            }
        )
    )
    os.utime(state, (used, used))
    return state


def _manager(
    tmp_path: Path, store: FakeStore | None = None, *, docker: FakeDocker | None = None
) -> ScopedSandboxManager:
    """A manager whose reclaiming, and the reaper's passes, reach ``docker``."""
    return ScopedSandboxManager(
        SandboxConfig(),
        store=store or FakeStore(),
        policies=Mock(),
        state_root=tmp_path / "states",
        client_factory=lambda: docker,
    )


def _reaper(manager: ScopedSandboxManager, policy: ScopeReaperPolicy | None = None) -> ScopeReaper:
    return ScopeReaper(manager, policy or ScopeReaperPolicy(), clock=lambda: NOW)


def _instance(manager: ScopedSandboxManager, project: Path) -> IsolatedSandbox:
    instance = IsolatedSandbox(
        manager.config,
        store=None,
        revision=SandboxPolicyRevision(
            ref=SandboxPolicyRef(id=uuid4(), revision=1),
            policy=SandboxPolicy(project_root=str(project)),
        ),
        name=SCOPE,
        roots=(str(project),),
        state_root=manager.state_root,
    )
    manager.instances[SCOPE] = instance
    return instance


@pytest.fixture
def relays(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    stopped: list[Path] = []
    monkeypatch.setattr(scope_layout, "stop_mcp_relay", stopped.append)
    return stopped


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "worktree"
    path.mkdir()
    return path


async def test_dead_scope_loses_containers_but_keeps_session_state(
    tmp_path: Path, project: Path, relays: list[Path]
) -> None:
    store = FakeStore(SCOPE)
    docker = FakeDocker()
    manager = _manager(tmp_path, store, docker=docker)
    # A Docker Desktop restart left the split unstartable; its gateway runs on.
    docker.add_scope(SCOPE, main="exited", gateway="running")
    state = _state(manager.state_root, SCOPE, mounted=project, used=NOW - HOUR)

    report = await _reaper(manager).reap_once()

    assert report == ReapReport(stopped=(SCOPE,))
    assert docker.containers.items == {} and docker.networks.items == {}
    assert set(docker.volumes.items) == {SCOPE + "-home", SCOPE + "-data"}
    assert state.is_dir() and store.rows == {SCOPE}
    assert relays == [state]


async def test_healthy_scope_keeps_its_containers_until_idle(
    tmp_path: Path, project: Path, relays: list[Path]
) -> None:
    docker = FakeDocker()
    manager = _manager(tmp_path, docker=docker)
    docker.add_scope(SCOPE)
    _state(manager.state_root, SCOPE, mounted=project, used=NOW - HOUR)
    docker.add_scope(OTHER)
    _state(manager.state_root, OTHER, mounted=project, used=NOW - 2 * DAY)

    report = await _reaper(manager).reap_once()

    assert report == ReapReport(stopped=(OTHER,))
    assert set(docker.containers.items) == {SCOPE, SCOPE + "-gateway"}
    assert set(docker.networks.items) == {SCOPE + "-network"}
    assert len(docker.volumes.items) == 4


async def test_recent_use_postpones_reaping(
    tmp_path: Path, project: Path, relays: list[Path]
) -> None:
    docker = FakeDocker()
    manager = _manager(tmp_path, docker=docker)
    docker.add_scope(SCOPE)
    state = _state(manager.state_root, SCOPE, mounted=project, used=NOW - 2 * DAY)
    layout = ScopeLayout(SCOPE, manager.state_root)
    layout.touch()
    os.utime(layout.last_used_file, (NOW - HOUR, NOW - HOUR))
    os.utime(state, (NOW - 2 * DAY, NOW - 2 * DAY))

    assert await _reaper(manager).reap_once() == ReapReport()
    assert len(docker.containers.items) == 2


async def test_scope_whose_worktree_is_gone_is_purged(
    tmp_path: Path, project: Path, relays: list[Path]
) -> None:
    store = FakeStore(SCOPE)
    docker = FakeDocker()
    manager = _manager(tmp_path, store, docker=docker)
    docker.add_scope(SCOPE)
    state = _state(manager.state_root, SCOPE, mounted=project, used=NOW - HOUR)
    project.rmdir()

    report = await _reaper(manager).reap_once()

    assert report == ReapReport(purged=(SCOPE,))
    assert docker.containers.items == {}
    assert docker.networks.items == {}
    assert docker.volumes.items == {}
    assert not state.exists() and store.rows == set()
    # The lock outlives the scope, so a waiting preparer never holds a removed one.
    assert (manager.state_root / ".locks" / (SCOPE + ".lock")).exists()


async def test_purge_ttl_reclaims_session_state_unless_disabled(
    tmp_path: Path, project: Path, relays: list[Path]
) -> None:
    docker = FakeDocker()
    manager = _manager(tmp_path, docker=docker)
    docker.add_scope(SCOPE, main=None, gateway=None)
    del docker.networks.items[SCOPE + "-network"]
    _state(manager.state_root, SCOPE, mounted=project, used=NOW - 100 * DAY)

    kept = await _reaper(manager, ScopeReaperPolicy(purge_idle=0)).reap_once()
    assert kept == ReapReport()
    assert len(docker.volumes.items) == 2

    purged = await _reaper(manager).reap_once()
    assert purged == ReapReport(purged=(SCOPE,))
    assert docker.volumes.items == {}


async def test_scope_in_use_is_kept_and_marked_used(
    tmp_path: Path, project: Path, relays: list[Path], caplog: pytest.LogCaptureFixture
) -> None:
    docker = FakeDocker()
    manager = _manager(tmp_path, docker=docker)
    docker.add_scope(SCOPE, main="exited")
    _state(manager.state_root, SCOPE, mounted=project, used=NOW - 200 * DAY)
    project.rmdir()
    instance = _instance(manager, project)
    adapter = object()
    instance.acquire(adapter)
    instance.layout.last_used_file.unlink()
    reaper = _reaper(manager)

    assert await reaper.tick() == ReapReport()
    assert len(docker.containers.items) == 2
    assert instance.layout.last_used_file.exists()

    instance.release(adapter)
    assert not instance.in_use
    assert await reaper.tick() == ReapReport(purged=(SCOPE,))
    assert SCOPE not in manager.instances
    # Each pass also ran the image cleanup, which found nothing to remove.
    assert "sandbox image cleanup failed" not in caplog.text


async def test_stop_removes_a_configurations_containers_but_keeps_volumes_once_unused(
    tmp_path: Path, project: Path, relays: list[Path]
) -> None:
    revision = SandboxPolicyRevision(
        ref=SandboxPolicyRef(id=uuid4(), revision=1),
        policy=SandboxPolicy(project_root=str(project)),
    )
    policies = Mock()
    policies.resolve = AsyncMock(return_value=revision)
    store = FakeStore()
    docker = FakeDocker()
    manager = ScopedSandboxManager(
        SandboxConfig(mount_roots=(str(tmp_path),)),
        store=store,
        policies=policies,
        state_root=tmp_path / "states",
        client_factory=lambda: docker,
    )
    configuration = HarnessConfiguration(
        kind=HarnessKind.CODEX, working_directory=str(project), sandbox_policy=revision.ref
    )
    scope = await manager.for_configuration(configuration)
    docker.add_scope(scope.name)
    store.rows.add(scope.name)
    state = _state(manager.state_root, scope.name, mounted=project, used=NOW)
    first, second = object(), object()
    scope.acquire(first)
    scope.acquire(second)

    scope.release(first)
    assert not await manager.stop(configuration)
    assert len(docker.containers.items) == 2

    scope.release(second)
    assert await manager.stop(configuration)
    assert docker.containers.items == {}
    assert docker.networks.items == {}
    assert sorted(docker.volumes.items) == [scope.name + "-data", scope.name + "-home"]
    assert state.is_dir()
    assert store.rows == {scope.name}
    assert scope.name not in manager.instances


async def test_disabled_reaper_marks_use_but_reclaims_nothing(
    tmp_path: Path, project: Path, relays: list[Path]
) -> None:
    docker = FakeDocker()
    manager = _manager(tmp_path, docker=docker)
    docker.add_scope(SCOPE, main="exited")
    _state(manager.state_root, SCOPE, mounted=project, used=NOW - 200 * DAY)
    instance = _instance(manager, project)
    instance.acquire(object())
    instance.layout.last_used_file.unlink()
    policy = ScopeReaperPolicy(enabled=False)

    assert await _reaper(manager, policy).tick() == ReapReport()
    assert len(docker.containers.items) == 2
    assert instance.layout.last_used_file.exists()


async def test_scopes_owned_elsewhere_are_never_touched(
    tmp_path: Path, project: Path, relays: list[Path]
) -> None:
    docker = FakeDocker()
    manager = _manager(tmp_path, docker=docker)
    # Prepared under another state root, such as a live test's temporary one.
    docker.add_scope(OTHER, main="created", gateway=None)
    FakeResource(
        docker.containers.items, "tth-codex", status="exited", labels={"tth.sandbox": "tth-codex"}
    )
    foreign = manager.state_root / "not-a-scope"
    foreign.mkdir(parents=True)
    (manager.state_root / SCOPE).symlink_to(project)

    assert await _reaper(manager).reap_once() == ReapReport()
    assert set(docker.containers.items) == {OTHER, "tth-codex"}
    assert len(docker.volumes.items) == 2
    assert foreign.is_dir() and project.is_dir()


async def test_failed_purge_keeps_the_row_for_the_next_pass(
    tmp_path: Path, project: Path, relays: list[Path]
) -> None:
    store = FakeStore(SCOPE)
    docker = FakeDocker()
    manager = _manager(tmp_path, store, docker=docker)
    docker.add_scope(SCOPE)
    _state(manager.state_root, SCOPE, mounted=project, used=NOW - HOUR)
    project.rmdir()
    docker.volumes.items[SCOPE + "-home"].remove_error = APIError("volume is in use")
    reaper = _reaper(manager)

    assert await reaper.reap_once() == ReapReport()
    assert store.rows == {SCOPE} and docker.containers.items == {}

    docker.volumes.items[SCOPE + "-home"].remove_error = None
    assert await reaper.reap_once() == ReapReport(purged=(SCOPE,))
    assert store.rows == set() and docker.volumes.items == {}


async def test_unreachable_docker_skips_the_pass(tmp_path: Path) -> None:
    def unreachable() -> Any:
        raise RuntimeError("daemon down")

    manager = ScopedSandboxManager(
        SandboxConfig(),
        store=FakeStore(),
        policies=Mock(),
        state_root=tmp_path / "states",
        client_factory=unreachable,
    )
    reaper = ScopeReaper(manager, ScopeReaperPolicy())

    assert await reaper.reap_once() == ReapReport()


async def test_scope_resolution_waits_until_the_row_is_gone(
    tmp_path: Path, relays: list[Path]
) -> None:
    store = FakeStore(SCOPE)
    store.deleting = asyncio.Event()
    manager = _manager(tmp_path, store, docker=FakeDocker())

    reclaim = asyncio.create_task(manager.reclaim(SCOPE, purge=True))
    while SCOPE not in manager._retiring:
        await asyncio.sleep(0)
    assert not await manager.reclaim(SCOPE, purge=True)
    waiter = asyncio.shield(manager._retiring[SCOPE])
    await asyncio.sleep(0.01)
    # Docker resources are gone, but the row is not: resolution still waits.
    assert not waiter.done() and store.rows == {SCOPE}

    store.deleting.set()
    assert await reclaim
    await waiter
    assert store.rows == set()
    assert SCOPE not in manager._retiring


def test_reclaiming_waits_for_a_preparation_in_progress(tmp_path: Path, relays: list[Path]) -> None:
    layout = ScopeLayout(SCOPE, tmp_path)
    layout.state.mkdir()
    removed = threading.Event()

    def remove() -> None:
        layout.remove(FakeDocker(), purge=True)
        removed.set()

    with layout.locked():
        thread = threading.Thread(target=remove)
        thread.start()
        assert not removed.wait(0.1)
        assert layout.state.is_dir()
    thread.join(5)
    assert removed.is_set() and not layout.state.exists()


def test_policy_from_env() -> None:
    assert ScopeReaperPolicy.from_env({}) == ScopeReaperPolicy()
    policy = ScopeReaperPolicy.from_env(
        {
            "TTH_SANDBOX_REAPER": "0",
            "TTH_SANDBOX_REAP_INTERVAL_SECONDS": "60",
            "TTH_SANDBOX_CONTAINER_IDLE_SECONDS": "3600",
            "TTH_SANDBOX_PURGE_IDLE_SECONDS": "0",
        }
    )
    assert not policy.enabled
    assert (policy.interval, policy.container_idle, policy.purge_idle) == (60, 3600, 0)
    with pytest.raises(ValueError, match="container_idle"):
        ScopeReaperPolicy.from_env({"TTH_SANDBOX_CONTAINER_IDLE_SECONDS": "0"})
    with pytest.raises(ValueError, match="TTH_SANDBOX_PURGE_IDLE_SECONDS"):
        ScopeReaperPolicy.from_env({"TTH_SANDBOX_PURGE_IDLE_SECONDS": "soon"})
    with pytest.raises(ValueError, match="shorter than the container idle period"):
        ScopeReaperPolicy.from_env({"TTH_SANDBOX_CONTAINER_IDLE_SECONDS": "600"})


async def test_each_pass_removes_unneeded_images_after_reaping(
    tmp_path: Path, project: Path, relays: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker()
    manager = _manager(tmp_path, docker=docker)
    docker.add_scope(SCOPE, main="exited")
    _state(manager.state_root, SCOPE, mounted=project, used=NOW)
    collected: list[dict[str, Any]] = []

    def collect_garbage(client: Any, **options: Any) -> tuple[str, ...]:
        # Runs after reaping, so the dead scope's containers are already gone.
        assert client is docker and SCOPE not in docker.containers.items
        collected.append(options)
        return ("tth-codex-custom:" + "a" * 24,)

    monkeypatch.setattr(custom_images, "collect_garbage", collect_garbage)

    report = await _reaper(manager).tick()

    assert report == ReapReport(stopped=(SCOPE,), images=("tth-codex-custom:" + "a" * 24,))
    [options] = collected
    assert options["state_root"] == manager.state_root
    assert [layout.name for layout in options["scopes"]] == [SCOPE]


async def test_image_cleanup_failure_keeps_the_reaping_report(
    tmp_path: Path, project: Path, relays: list[Path], caplog: pytest.LogCaptureFixture
) -> None:
    docker = FakeDocker()
    manager = _manager(tmp_path, docker=docker)
    docker.add_scope(SCOPE, main="exited")
    _state(manager.state_root, SCOPE, mounted=project, used=NOW)
    manager.collect_images = AsyncMock(side_effect=RuntimeError("images unreachable"))

    assert await _reaper(manager).tick() == ReapReport(stopped=(SCOPE,))
    assert "sandbox image cleanup failed" in caplog.text


async def test_pass_without_docker_skips_reaping_and_image_cleanup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def unreachable() -> Any:
        raise RuntimeError("docker is not running")

    manager = _manager(tmp_path, docker=FakeDocker())
    manager.client_factory = unreachable
    manager.collect_images = AsyncMock()

    assert await _reaper(manager).tick() == ReapReport()

    manager.collect_images.assert_not_awaited()
    [record] = caplog.records
    assert "docker is unreachable" in record.message and record.exc_info is None


async def test_disabled_reaper_removes_no_images(tmp_path: Path) -> None:
    manager = _manager(tmp_path, docker=FakeDocker())
    manager.collect_images = AsyncMock()

    await _reaper(manager, ScopeReaperPolicy(enabled=False)).tick()

    manager.collect_images.assert_not_awaited()
