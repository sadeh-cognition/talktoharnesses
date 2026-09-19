"""SandboxConfig/SandboxManager and remote-registry cutover tests (no Docker)."""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError

from talktoharnesses.providers.registry import AdapterRegistry
from talktoharnesses.remote.adapter import RemoteHarnessAdapter
from talktoharnesses.remote.docker_ops import container_otlp_endpoint
from talktoharnesses.remote.registry import build_remote_adapter_registry
from talktoharnesses.remote.sandbox import (
    TOOLCHAIN_ENV,
    SandboxConfig,
    SandboxManager,
    SandboxRecordData,
    WorkspaceSetupOutcome,
    WorkspaceSetupStarted,
    ensure_docker_cli_available,
)
from talktoharnesses.remote.sandbox_auth import AUTH_FILE_DEFAULTS
from talktoharnesses.remote.sandbox_rtk import (
    RTK_INIT_SPECS,
    rtk_init_command,
    seed_rtk_config,
)


class FakeStore:
    """In-memory SandboxStore capturing every upsert."""

    def __init__(self) -> None:
        self.records: dict[HarnessKind, SandboxRecordData] = {}
        self.upserts: list[SandboxRecordData] = []

    async def get(self, kind: HarnessKind) -> SandboxRecordData | None:
        return self.records.get(kind)

    async def upsert(self, record: SandboxRecordData) -> None:
        self.records[record.kind] = record
        self.upserts.append(record)

    async def reserve(self, record: SandboxRecordData) -> SandboxRecordData:
        existing = self.records.get(record.kind)
        if existing is not None:
            return existing
        self.records[record.kind] = record
        return record


def _spawnable_manager(
    monkeypatch: pytest.MonkeyPatch,
    *,
    store: FakeStore | None = None,
    config: SandboxConfig | None = None,
) -> tuple[SandboxManager, list[tuple[str, Any]]]:
    """Manager whose Docker interactions are recorded instead of executed."""
    manager = SandboxManager(config or SandboxConfig.from_env({}), store=store)
    calls: list[tuple[str, Any]] = []

    def ensure_image(kind: HarnessKind) -> None:
        calls.append(("ensure_image", kind))

    def ensure_container(kind: HarnessKind, token: str) -> None:
        calls.append(("ensure_container", (kind, token)))

    async def wait_healthy(kind: HarnessKind, base_url: str) -> None:
        calls.append(("wait_healthy", (kind, base_url)))

    async def endpoint_healthy(kind: HarnessKind, endpoint: Any) -> bool:
        return True

    def container_running(name: str) -> bool:
        return True

    monkeypatch.setattr(manager, "_ensure_image", ensure_image)
    monkeypatch.setattr(manager, "_ensure_container", ensure_container)
    monkeypatch.setattr(manager, "_wait_healthy", wait_healthy)
    monkeypatch.setattr(manager, "_endpoint_healthy", endpoint_healthy)
    monkeypatch.setattr(manager, "_container_running", container_running)
    return manager, calls


async def test_endpoint_spawns_on_demand_and_persists_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore()
    manager, calls = _spawnable_manager(monkeypatch, store=store)

    endpoint = await manager.endpoint(HarnessKind.GROK)

    assert endpoint.base_url == "http://127.0.0.1:8111"
    assert endpoint.token
    assert [name for name, _ in calls] == ["ensure_image", "ensure_container", "wait_healthy"]
    record = store.records[HarnessKind.GROK]
    assert record.status == "ready"
    assert record.container_name == "tth-grok"
    assert record.host_port == 8111
    assert record.split_token == endpoint.token
    assert record.last_ready_at is not None
    # Statuses were written in order: preparing, then ready.
    assert [r.status for r in store.upserts] == ["preparing", "ready"]

    # Second resolution is served from the cache without touching Docker.
    calls.clear()
    assert await manager.endpoint(HarnessKind.GROK) is endpoint
    assert calls == []


async def test_endpoint_reuses_persisted_token_on_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore()
    now = datetime.now(UTC)
    store.records[HarnessKind.GROK] = SandboxRecordData(
        kind=HarnessKind.GROK,
        container_name="tth-grok",
        image="tth-grok:latest",
        host_port=8111,
        base_url="http://127.0.0.1:8111",
        split_token="persisted-token",
        status="ready",
        created_at=now,
        updated_at=now,
        last_ready_at=now,
    )
    manager, calls = _spawnable_manager(monkeypatch, store=store)

    endpoint = await manager.endpoint(HarnessKind.GROK)

    assert endpoint.token == "persisted-token"
    ensure_calls = [args for name, args in calls if name == "ensure_container"]
    assert ensure_calls == [(HarnessKind.GROK, "persisted-token")]
    assert store.records[HarnessKind.GROK].created_at == now


async def test_cached_endpoint_is_reprepared_when_health_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, calls = _spawnable_manager(monkeypatch, store=FakeStore())
    first = await manager.endpoint(HarnessKind.GROK)
    calls.clear()

    async def unhealthy(kind: HarnessKind, endpoint: Any) -> bool:
        return False

    monkeypatch.setattr(manager, "_endpoint_healthy", unhealthy)
    second = await manager.endpoint(HarnessKind.GROK)

    assert second is not first
    assert [name for name, _ in calls] == ["ensure_image", "ensure_container", "wait_healthy"]


async def test_cached_endpoint_refreshes_changed_auth_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("first", encoding="utf-8")
    config = SandboxConfig(auth_files={HarnessKind.GROK: str(auth_file)})
    manager, calls = _spawnable_manager(monkeypatch, store=FakeStore(), config=config)
    first = await manager.endpoint(HarnessKind.GROK)
    calls.clear()

    auth_file.write_text("rotated-credentials", encoding="utf-8")
    second = await manager.endpoint(HarnessKind.GROK)

    assert second is not first
    assert [name for name, _ in calls] == ["ensure_image", "ensure_container", "wait_healthy"]


async def test_prepare_failure_marks_record_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore()
    manager, _ = _spawnable_manager(monkeypatch, store=store)

    def boom(kind: HarnessKind) -> None:
        raise DomainError(
            ErrorCode.SANDBOX_UNAVAILABLE,
            "build failed",
            details={"kind": kind.value, "reason": "image_build_failed"},
        )

    monkeypatch.setattr(manager, "_ensure_image", boom)

    with pytest.raises(DomainError) as excinfo:
        await manager.endpoint(HarnessKind.GROK)

    assert excinfo.value.code is ErrorCode.SANDBOX_UNAVAILABLE
    assert excinfo.value.details["reason"] == "image_build_failed"
    assert store.records[HarnessKind.GROK].status == "failed"

    # A later call retries the preparation instead of caching the failure.
    calls_after: list[HarnessKind] = []
    monkeypatch.setattr(manager, "_ensure_image", calls_after.append)
    endpoint = await manager.endpoint(HarnessKind.GROK)
    assert calls_after == [HarnessKind.GROK]
    assert endpoint.base_url == "http://127.0.0.1:8111"
    assert store.records[HarnessKind.GROK].status == "ready"


async def test_slow_prepare_raises_sandbox_preparing_then_attaches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore()
    config = SandboxConfig.from_env({}).model_copy(update={"prepare_grace": 0.05})
    manager, _ = _spawnable_manager(monkeypatch, store=store, config=config)
    release = asyncio.Event()
    prepares = 0

    async def slow_wait(kind: HarnessKind, base_url: str) -> None:
        nonlocal prepares
        prepares += 1
        await release.wait()

    monkeypatch.setattr(manager, "_wait_healthy", slow_wait)

    with pytest.raises(DomainError) as excinfo:
        await manager.endpoint(HarnessKind.GROK)
    assert excinfo.value.code is ErrorCode.SANDBOX_PREPARING

    release.set()
    endpoint = await manager.endpoint(HarnessKind.GROK)
    assert endpoint.base_url == "http://127.0.0.1:8111"
    # The retry joined the in-flight preparation instead of starting another.
    assert prepares == 1


async def test_endpoint_rejects_paths_outside_mount_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = SandboxConfig(mount_roots=(str(tmp_path),))
    manager, calls = _spawnable_manager(monkeypatch, config=config)

    with pytest.raises(DomainError) as excinfo:
        await manager.endpoint(HarnessKind.GROK, ("/srv/elsewhere",))

    assert excinfo.value.code is ErrorCode.SANDBOX_PATH_NOT_MOUNTED
    assert excinfo.value.details["reason"] == "path_not_mounted"
    assert excinfo.value.details["path"] == "/srv/elsewhere"
    assert excinfo.value.details["mount_roots"] == [str(tmp_path)]
    # Rejected before any Docker work.
    assert calls == []

    endpoint = await manager.endpoint(HarnessKind.GROK, (str(tmp_path / "project"),))
    assert endpoint.base_url == "http://127.0.0.1:8111"


def test_create_container_always_bind_mounts_roots(tmp_path: Path) -> None:
    runs: list[dict[str, Any]] = []

    class Containers:
        def run(self, image: str, **kwargs: Any) -> None:
            runs.append({"image": image, **kwargs})

    def mount_type(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    manager = SandboxManager(SandboxConfig(mount_roots=(str(tmp_path),)))
    client: Any = SimpleNamespace(containers=Containers())
    manager._create_container(  # pyright: ignore[reportPrivateUsage]
        client,
        mount_type,
        kind=HarnessKind.GROK,
        name="tth-grok",
        image="tth-grok:latest",
        environment={"TTH_SPLIT_TOKEN": "tok"},
    )

    assert len(runs) == 1
    assert {
        "target": str(tmp_path),
        "source": str(tmp_path),
        "type": "bind",
    } in runs[0]["mounts"]
    assert runs[0]["security_opt"] == ["no-new-privileges:true"]
    # grok's multi-threaded CLI needs more pid headroom than the other kinds.
    assert runs[0]["pids_limit"] == 2048


def test_create_codex_container_allows_nested_sandbox(tmp_path: Path) -> None:
    runs: list[dict[str, Any]] = []

    class Containers:
        def run(self, image: str, **kwargs: Any) -> None:
            runs.append({"image": image, **kwargs})

    def mount_type(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    manager = SandboxManager(SandboxConfig(mount_roots=(str(tmp_path),)))
    client: Any = SimpleNamespace(containers=Containers())
    manager._create_container(  # pyright: ignore[reportPrivateUsage]
        client,
        mount_type,
        kind=HarnessKind.CODEX,
        name="tth-codex",
        image="tth-codex:latest",
        environment={"TTH_SPLIT_TOKEN": "tok"},
    )

    assert runs[0]["security_opt"] == [
        "no-new-privileges:true",
        "seccomp=unconfined",
    ]
    assert runs[0]["pids_limit"] == 512


async def test_two_managers_sharing_one_store_reuse_one_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent workers must adopt one split token, not recreate each other's container."""
    store = FakeStore()
    manager_a, calls_a = _spawnable_manager(monkeypatch, store=store)
    manager_b, calls_b = _spawnable_manager(monkeypatch, store=store)

    first, second = await asyncio.gather(
        manager_a.endpoint(HarnessKind.GROK), manager_b.endpoint(HarnessKind.GROK)
    )

    assert first.token == second.token
    container_tokens = {
        args[1]
        for calls in (calls_a, calls_b)
        for name, args in calls
        if name == "ensure_container"
    }
    assert container_tokens == {first.token}
    assert store.records[HarnessKind.GROK].split_token == first.token


async def test_wait_healthy_requires_matching_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 from the wrong split (or a leftover process) must not mark ready."""
    reported_kind = "cursor"

    class FakeResponse:
        status_code = 200

        def json(self) -> dict[str, str]:
            return {"kind": reported_kind}

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def get(self, path: str) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    config = SandboxConfig(health_timeout=0.05, health_poll_interval=0.01)
    manager = SandboxManager(config)

    with pytest.raises(DomainError) as excinfo:
        await manager._wait_healthy(  # pyright: ignore[reportPrivateUsage]
            HarnessKind.GROK, "http://127.0.0.1:1"
        )
    assert excinfo.value.code is ErrorCode.SANDBOX_UNAVAILABLE
    assert "kind mismatch" in excinfo.value.details["last_error"]

    reported_kind = "grok"
    await manager._wait_healthy(  # pyright: ignore[reportPrivateUsage]
        HarnessKind.GROK, "http://127.0.0.1:1"
    )


def test_environment_passthrough_cannot_override_managed_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TTH_SPLIT_TOKEN", "operator-token")
    monkeypatch.setenv("XAI_API_KEY", "provider")
    config = SandboxConfig.from_env(
        {"TTH_SANDBOX_ENV_GROK": "TTH_SPLIT_TOKEN,OTEL_EXPORTER_OTLP_HEADERS,XAI_API_KEY"}
    )
    manager = SandboxManager(config)

    environment = manager._environment(  # pyright: ignore[reportPrivateUsage]
        HarnessKind.GROK, "minted-token"
    )

    assert environment["TTH_SPLIT_TOKEN"] == "minted-token"
    assert environment["XAI_API_KEY"] == "provider"


async def test_concurrent_endpoint_calls_share_one_prepare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _ = _spawnable_manager(monkeypatch, store=FakeStore())
    prepares = 0

    async def counting_wait(kind: HarnessKind, base_url: str) -> None:
        nonlocal prepares
        prepares += 1
        await asyncio.sleep(0)

    monkeypatch.setattr(manager, "_wait_healthy", counting_wait)

    first, second = await asyncio.gather(
        manager.endpoint(HarnessKind.GROK), manager.endpoint(HarnessKind.GROK)
    )
    assert first == second
    assert prepares == 1


def _no_which(cmd: str) -> None:
    return None


def test_ensure_docker_cli_available_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", _no_which)
    with pytest.raises(DomainError) as excinfo:
        ensure_docker_cli_available()
    assert excinfo.value.code is ErrorCode.SANDBOX_UNAVAILABLE
    assert excinfo.value.details == {"reason": "docker_unavailable"}


def test_ensure_docker_cli_available_includes_kind_in_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", _no_which)
    with pytest.raises(DomainError) as excinfo:
        ensure_docker_cli_available(HarnessKind.GROK)
    assert excinfo.value.details == {"reason": "docker_unavailable", "kind": "grok"}


def test_ensure_docker_cli_available_returns_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_which(cmd: str) -> str:
        return "/usr/local/bin/docker"

    monkeypatch.setattr(shutil, "which", fake_which)
    assert ensure_docker_cli_available() == "/usr/local/bin/docker"


def test_missing_build_context_raises_actionable_error(tmp_path: Path) -> None:
    manager = SandboxManager(SandboxConfig.from_env({}))

    # The editable-install repo root actually has the build contexts, so fake
    # the package location to prove the wheel-install failure mode.
    import talktoharnesses as pkg

    original = pkg.__file__
    try:
        pkg.__file__ = str(tmp_path / "lib" / "talktoharnesses" / "__init__.py")
        with pytest.raises(DomainError) as excinfo:
            manager._resolve_build_root(HarnessKind.GROK)  # pyright: ignore[reportPrivateUsage]
    finally:
        pkg.__file__ = original

    assert excinfo.value.code is ErrorCode.SANDBOX_UNAVAILABLE
    assert excinfo.value.details["reason"] == "build_context_missing"


async def test_is_running_never_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore()
    manager, calls = _spawnable_manager(monkeypatch, store=store)

    # No record: not running, and nothing was spawned.
    assert await manager.is_running(HarnessKind.GROK) is False
    assert calls == []

    # A cached endpoint still checks Docker but never spawns.
    await manager.endpoint(HarnessKind.GROK)
    calls.clear()
    assert await manager.is_running(HarnessKind.GROK) is True
    assert calls == []


def test_config_from_env_uses_default_ports() -> None:
    config = SandboxConfig.from_env({})
    assert config.ports[HarnessKind.CLAUDE] == 8114
    assert config.ports[HarnessKind.GROK] == 8111


def test_config_from_env_reads_ports_tag_and_passthrough() -> None:
    config = SandboxConfig.from_env(
        {
            "TTH_SPLIT_PORT_GROK": "9111",
            "TTH_SANDBOX_IMAGE_TAG": "v2",
            "HOME": "/home/agent-host",
            "TTH_SANDBOX_ENV_GROK": "XAI_API_KEY, EXTRA_KEY",
        }
    )
    assert config.ports[HarnessKind.GROK] == 9111
    assert config.image_tag == "v2"
    assert config.mount_roots == ("/home/agent-host/dev",)
    assert config.env_passthrough[HarnessKind.GROK] == ("XAI_API_KEY", "EXTRA_KEY")
    assert config.forward_otel_headers is False


def test_config_from_env_without_home_mounts_nothing() -> None:
    assert SandboxConfig.from_env({}).mount_roots == ()


def test_config_from_env_reads_explicit_mount_roots() -> None:
    config = SandboxConfig.from_env(
        {
            "HOME": "/home/agent-host",
            "TTH_SANDBOX_MOUNT_ROOTS": "/srv/projects: /home/agent-host/work :",
        }
    )
    assert config.mount_roots == ("/srv/projects", "/home/agent-host/work")


def test_config_from_env_empty_mount_roots_mounts_nothing() -> None:
    config = SandboxConfig.from_env({"HOME": "/home/agent-host", "TTH_SANDBOX_MOUNT_ROOTS": ""})
    assert config.mount_roots == ()


def test_config_from_env_discovers_provider_auth_files(tmp_path: Path) -> None:
    expected: dict[HarnessKind, str] = {}
    for kind, spec in AUTH_FILE_DEFAULTS.items():
        auth_file = tmp_path / spec.default_relative_path
        auth_file.parent.mkdir(parents=True, exist_ok=True)
        auth_file.write_text(f"test-{kind.value}-auth", encoding="utf-8")
        expected[kind] = str(auth_file)

    config = SandboxConfig.from_env({"HOME": str(tmp_path)})

    assert config.auth_files == expected


def test_config_from_env_auth_file_overrides(tmp_path: Path) -> None:
    default_cursor_auth = tmp_path / ".config" / "cursor" / "auth.json"
    default_cursor_auth.parent.mkdir(parents=True)
    default_cursor_auth.write_text("default-auth", encoding="utf-8")
    custom_grok_auth = tmp_path / "custom-grok-auth.json"

    config = SandboxConfig.from_env(
        {
            "HOME": str(tmp_path),
            "TTH_SANDBOX_GROK_AUTH_FILE": str(custom_grok_auth),
            "TTH_SANDBOX_CURSOR_AUTH_FILE": "",
        }
    )

    assert config.auth_files.get(HarnessKind.GROK) == str(custom_grok_auth)
    assert HarnessKind.CURSOR not in config.auth_files


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "http://host.docker.internal:4318"),
        ("", "http://host.docker.internal:4318"),
        ("   ", "http://host.docker.internal:4318"),
        ("false", "false"),
        ("0", "0"),
        ("http://localhost:4318", "http://host.docker.internal:4318"),
        ("http://127.0.0.1:4319/v1/traces", "http://host.docker.internal:4319/v1/traces"),
        ("http://localhost/v1", "http://host.docker.internal/v1"),
        ("https://collector.example.com:443", "https://collector.example.com:443"),
        ("  http://localhost:4318  ", "http://host.docker.internal:4318"),
    ],
)
def test_container_otlp_endpoint_rewrite(raw: str | None, expected: str) -> None:
    assert container_otlp_endpoint(raw) == expected


def test_environment_always_manages_otel_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = SandboxManager(SandboxConfig.from_env({}))
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    monkeypatch.setenv("OTEL_SERVICE_NAME", "proxy-name")

    environment = manager._environment(  # pyright: ignore[reportPrivateUsage]
        HarnessKind.CLAUDE, "tok"
    )
    assert environment["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://host.docker.internal:4318"
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in environment
    # Each split bakes its own service name; the proxy's must not leak in.
    assert "OTEL_SERVICE_NAME" not in environment

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4319")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer x")
    environment = manager._environment(  # pyright: ignore[reportPrivateUsage]
        HarnessKind.CLAUDE, "tok"
    )
    assert environment["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://host.docker.internal:4319"
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in environment

    opted_in = SandboxManager(SandboxConfig.from_env({"TTH_SANDBOX_FORWARD_OTEL_HEADERS": "1"}))
    environment = opted_in._environment(  # pyright: ignore[reportPrivateUsage]
        HarnessKind.CLAUDE, "tok"
    )
    assert environment["OTEL_EXPORTER_OTLP_HEADERS"] == "authorization=Bearer x"

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "0")
    environment = manager._environment(  # pyright: ignore[reportPrivateUsage]
        HarnessKind.CLAUDE, "tok"
    )
    assert environment["OTEL_EXPORTER_OTLP_ENDPOINT"] == "0"


def test_create_container_adds_host_gateway_mapping(tmp_path: Path) -> None:
    runs: list[dict[str, Any]] = []

    class Containers:
        def run(self, image: str, **kwargs: Any) -> None:
            runs.append({"image": image, **kwargs})

    def mount_type(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    manager = SandboxManager(SandboxConfig(mount_roots=(str(tmp_path),)))
    client: Any = SimpleNamespace(containers=Containers())
    manager._create_container(  # pyright: ignore[reportPrivateUsage]
        client,
        mount_type,
        kind=HarnessKind.GROK,
        name="tth-grok",
        image="tth-grok:latest",
        environment={"TTH_SPLIT_TOKEN": "tok"},
    )

    assert runs[0]["extra_hosts"] == {"host.docker.internal": "host-gateway"}


def test_container_match_checks_managed_runtime_configuration(tmp_path: Path) -> None:
    config = SandboxConfig(
        ports={HarnessKind.GROK: 9111},
        image_tag="latest",
        mount_roots=(str(tmp_path),),
        env_passthrough={HarnessKind.GROK: ("XAI_API_KEY",)},
    )
    manager = SandboxManager(config)
    container: Any = SimpleNamespace(
        image=SimpleNamespace(tags=["tth-grok:latest"]),
        attrs={
            "Config": {
                "Env": [
                    "TTH_SPLIT_TOKEN=split",
                    "XAI_API_KEY=provider",
                    "OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4318",
                ]
            },
            "HostConfig": {
                "PortBindings": {"8010/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9111"}]},
                "SecurityOpt": ["no-new-privileges:true"],
                "PidsLimit": 2048,
                "ExtraHosts": ["host.docker.internal:host-gateway"],
            },
            "Mounts": [
                {
                    "Destination": "/home/agent",
                    "Name": "tth-grok-home",
                    "Type": "volume",
                    "Source": "/var/lib/docker/volumes/tth-grok-home/_data",
                },
                {
                    "Destination": "/data",
                    "Name": "tth-grok-data",
                    "Type": "volume",
                    "Source": "/var/lib/docker/volumes/tth-grok-data/_data",
                },
                {
                    "Destination": str(tmp_path),
                    "Type": "bind",
                    "Source": "/run/desktop/mnt/host/rewritten-projects-path",
                },
            ],
        },
    )

    def matches(
        *,
        image: str = "tth-grok:latest",
        token: str = "split",
        otlp_endpoint: str = "http://host.docker.internal:4318",
    ) -> bool:
        return manager._container_matches(  # pyright: ignore[reportPrivateUsage]
            container,
            HarnessKind.GROK,
            image=image,
            name="tth-grok",
            environment={
                "TTH_SPLIT_TOKEN": token,
                "XAI_API_KEY": "provider",
                "OTEL_EXPORTER_OTLP_ENDPOINT": otlp_endpoint,
            },
        )

    assert matches()
    assert not matches(image="tth-grok:v2")

    container.attrs["HostConfig"]["PortBindings"]["8010/tcp"][0]["HostPort"] = "9222"
    assert not matches()
    container.attrs["HostConfig"]["PortBindings"]["8010/tcp"][0]["HostPort"] = "9111"

    container.attrs["Mounts"][0]["Name"] = "other-home"
    assert not matches()
    container.attrs["Mounts"][0]["Name"] = "tth-grok-home"

    container.attrs["Mounts"][2]["Destination"] = "/other-projects"
    assert not matches()
    container.attrs["Mounts"][2]["Destination"] = str(tmp_path)

    container.attrs["HostConfig"]["SecurityOpt"] = []
    assert not matches()
    container.attrs["HostConfig"]["SecurityOpt"] = ["no-new-privileges:true"]

    # A container created before the per-kind pid budget is recreated.
    container.attrs["HostConfig"]["PidsLimit"] = 512
    assert not matches()
    container.attrs["HostConfig"]["PidsLimit"] = 2048

    assert not matches(token="changed")

    # OTel endpoint drift or a missing host-gateway mapping forces recreation.
    assert not matches(otlp_endpoint="http://host.docker.internal:9999")
    container.attrs["HostConfig"]["ExtraHosts"] = None
    assert not matches()
    container.attrs["HostConfig"]["ExtraHosts"] = ["host.docker.internal:host-gateway"]
    assert matches()


@pytest.mark.parametrize(
    ("kind", "target_directory", "target_filename"),
    [
        (HarnessKind.GROK, "/home/agent/.grok", "auth.json"),
        (HarnessKind.CURSOR, "/home/agent/.config/cursor", "auth.json"),
        (HarnessKind.CODEX, "/home/agent/.codex", "auth.json"),
        (HarnessKind.CLAUDE, "/home/agent/.claude", ".credentials.json"),
        (HarnessKind.OPENCODE, "/home/agent/.local/share/opencode", "auth.json"),
        (HarnessKind.PRIME_AGENT, "/home/agent/.prime", "config.json"),
        (HarnessKind.MUSE, "/home/agent/.config/muse", "auth.json"),
    ],
)
def test_seed_provider_auth_copies_only_auth_file(
    tmp_path: Path,
    kind: HarnessKind,
    target_directory: str,
    target_filename: str,
) -> None:
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("test-auth", encoding="utf-8")
    calls: list[dict[str, Any]] = []

    class Containers:
        def run(self, image: str, **kwargs: Any) -> None:
            calls.append({"image": image, **kwargs})

    def mount_type(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    config = SandboxConfig(auth_files={kind: str(auth_file)})
    manager = SandboxManager(config)
    client: Any = SimpleNamespace(containers=Containers())
    manager._seed_auth_file(  # pyright: ignore[reportPrivateUsage]
        client,
        mount_type,
        kind=kind,
        image=f"tth-{kind.value}:latest",
        name=f"tth-{kind.value}",
    )

    assert len(calls) == 1
    assert calls[0]["image"] == f"tth-{kind.value}:latest"
    assert target_directory in calls[0]["command"][2]
    assert target_filename in calls[0]["command"][2]
    assert "chmod(0o600)" in calls[0]["command"][2]
    assert calls[0]["mounts"] == [
        {
            "target": f"/seed/{target_filename}",
            "source": str(auth_file),
            "type": "bind",
            "read_only": True,
        },
        {
            "target": "/home/agent",
            "source": f"tth-{kind.value}-home",
            "type": "volume",
        },
    ]
    assert calls[0]["network_disabled"] is True
    assert calls[0]["remove"] is True


@pytest.mark.parametrize("kind", list(HarnessKind))
def test_seed_provider_auth_rejects_missing_configured_file(
    tmp_path: Path,
    kind: HarnessKind,
) -> None:
    missing = str(tmp_path / "missing.json")
    config = SandboxConfig(auth_files={kind: missing})
    manager = SandboxManager(config)

    with pytest.raises(DomainError) as excinfo:
        manager._seed_auth_file(  # pyright: ignore[reportPrivateUsage]
            SimpleNamespace(),
            SimpleNamespace(),
            kind=kind,
            image=f"tth-{kind.value}:latest",
            name=f"tth-{kind.value}",
        )

    assert excinfo.value.code is ErrorCode.SANDBOX_UNAVAILABLE
    assert excinfo.value.details["reason"] == "auth_file_missing"


@pytest.mark.parametrize(
    ("kind", "directories", "init_args", "rules_file"),
    [
        (HarnessKind.CODEX, [".codex"], ["--codex"], ".codex/AGENTS.md"),
        (HarnessKind.GROK, [".codex", ".grok"], ["--codex"], ".grok/AGENTS.md"),
        (HarnessKind.MUSE, [".codex"], ["--codex"], ".codex/AGENTS.md"),
        (HarnessKind.CURSOR, [".claude", ".cursor"], ["--agent", "cursor", "--auto-patch"], None),
        (
            HarnessKind.OPENCODE,
            [".config/opencode/plugins"],
            ["--opencode", "--auto-patch"],
            None,
        ),
    ],
)
def test_seed_rtk_config_runs_rtk_init_against_home_volume(
    kind: HarnessKind, directories: list[str], init_args: list[str], rules_file: str | None
) -> None:
    calls: list[dict[str, Any]] = []

    class Containers:
        def run(self, image: str, **kwargs: Any) -> None:
            calls.append({"image": image, **kwargs})

    def mount_type(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    client: Any = SimpleNamespace(containers=Containers())
    seeded = seed_rtk_config(
        client,
        mount_type,
        kind=kind,
        image=f"tth-{kind.value}:latest",
        home_volume=f"tth-{kind.value}-home",
    )

    assert seeded is True
    assert len(calls) == 1
    assert calls[0]["image"] == f"tth-{kind.value}:latest"
    assert calls[0]["command"][:2] == ["python", "-c"]
    script = calls[0]["command"][2]
    for directory in directories:
        assert f"(Path.home() / {directory!r}).mkdir(parents=True, exist_ok=True)" in script
    assert f"subprocess.run({['rtk', 'init', '--global', *init_args]!r}, check=True)" in script
    # Rules-file kinds get the Codex rules text inlined into their own file.
    if rules_file is None:
        assert "AGENTS.md" not in script
    else:
        assert f"target = Path.home() / {rules_file!r}" in script
    assert calls[0]["environment"] == {"HOME": "/home/agent"}
    assert calls[0]["mounts"] == [
        {"target": "/home/agent", "source": f"tth-{kind.value}-home", "type": "volume"}
    ]
    assert calls[0]["network_disabled"] is True
    assert calls[0]["cap_drop"] == ["ALL"]
    assert calls[0]["remove"] is True


@pytest.mark.parametrize(
    ("kind", "rules_file"),
    [
        (HarnessKind.CODEX, ".codex/AGENTS.md"),
        (HarnessKind.GROK, ".grok/AGENTS.md"),
        (HarnessKind.MUSE, ".codex/AGENTS.md"),
    ],
)
def test_rtk_init_script_inlines_codex_rules_once(
    kind: HarnessKind, rules_file: str, tmp_path: Path
) -> None:
    """Seeding runs on every preparation; the rules must not accumulate.

    ``rtk init --codex`` re-appends its ``@RTK.md`` reference whenever the
    reference is missing, so a naive inline would add a copy each time.
    """
    import subprocess
    import sys

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".grok").mkdir()
    (home / rules_file).write_text("# Mine\n\nKeep it.\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_rtk = fake_bin / "rtk"
    fake_rtk.write_text(
        "#!/bin/sh\nset -e\n"
        "printf '# RTK\\n\\nAlways prefix shell commands with `rtk`.\\n'"
        ' > "$HOME/.codex/RTK.md"\n'
        'printf \'@%s/.codex/RTK.md\\n\' "$HOME" >> "$HOME/.codex/AGENTS.md"\n'
    )
    fake_rtk.chmod(0o755)
    command = rtk_init_command(kind)
    assert command is not None
    env = {"HOME": str(home), "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    for _ in range(2):
        subprocess.run([sys.executable, *command[1:]], check=True, env=env)

    assert (home / rules_file).read_text() == (
        "# Mine\n\nKeep it.\n\n# RTK\n\nAlways prefix shell commands with `rtk`.\n"
    )


@pytest.mark.parametrize("kind", [kind for kind in HarnessKind if kind not in RTK_INIT_SPECS])
def test_seed_rtk_config_skips_kinds_without_seeded_files(kind: HarnessKind) -> None:
    class Containers:
        def run(self, image: str, **kwargs: Any) -> None:
            raise AssertionError("no seeding container expected")

    client: Any = SimpleNamespace(containers=Containers())
    assert (
        seed_rtk_config(
            client,
            SimpleNamespace(),
            kind=kind,
            image=f"tth-{kind.value}:latest",
            home_volume=f"tth-{kind.value}-home",
        )
        is False
    )


def test_seed_rtk_config_fails_open_on_docker_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from docker.errors import DockerException

    class Containers:
        def run(self, image: str, **kwargs: Any) -> None:
            raise DockerException(f"{kwargs['command']} exited 2 in {image}: rtk exploded")

    def mount_type(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    client: Any = SimpleNamespace(containers=Containers())
    with caplog.at_level("WARNING"):
        seeded = seed_rtk_config(
            client,
            mount_type,
            kind=HarnessKind.CODEX,
            image="tth-codex:latest",
            home_volume="tth-codex-home",
        )

    assert seeded is False
    assert "rtk seeding for codex failed" in caplog.text


def test_ensure_container_seeds_rtk_after_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = SandboxManager(SandboxConfig())
    client = SimpleNamespace()
    order: list[str] = []

    def docker_client(kind: HarnessKind) -> Any:
        return client

    def seed_auth_file(client: Any, mount: Any, **kwargs: Any) -> None:
        order.append("auth")

    def seed_rtk(
        client: Any, mount: Any, *, kind: HarnessKind, image: str, home_volume: str
    ) -> bool:
        order.append(f"rtk:{kind.value}:{image}:{home_volume}")
        return True

    def reconcile_container(*args: Any, **kwargs: Any) -> None:
        order.append("reconcile")

    monkeypatch.setattr(manager, "_docker_client", docker_client)
    monkeypatch.setattr(manager, "_seed_auth_file", seed_auth_file)
    monkeypatch.setattr("talktoharnesses.remote.sandbox_rtk.seed_rtk_config", seed_rtk)
    monkeypatch.setattr(manager, "_reconcile_container", reconcile_container)

    manager._ensure_container(HarnessKind.CURSOR, "token")  # pyright: ignore[reportPrivateUsage]

    assert order == ["auth", "rtk:cursor:tth-cursor:latest:tth-cursor-home", "reconcile"]


def _skip_rtk_seeding(monkeypatch: pytest.MonkeyPatch) -> None:
    """For container tests whose bare fake client cannot run the RTK seeder."""
    monkeypatch.setattr(
        "talktoharnesses.remote.sandbox_rtk.seed_rtk_config", lambda *args, **kwargs: True
    )


def test_ensure_container_refreshes_auth_for_matching_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _skip_rtk_seeding(monkeypatch)
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("rotated", encoding="utf-8")
    manager = SandboxManager(SandboxConfig(auth_files={HarnessKind.GROK: str(auth_file)}))
    client = SimpleNamespace()
    seeded: list[HarnessKind] = []

    def docker_client(kind: HarnessKind) -> Any:
        return client

    def seed_auth_file(
        client: Any,
        mount: Any,
        *,
        kind: HarnessKind,
        image: str,
        name: str,
    ) -> None:
        seeded.append(kind)

    def reconcile_container(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(manager, "_docker_client", docker_client)
    monkeypatch.setattr(manager, "_seed_auth_file", seed_auth_file)
    monkeypatch.setattr(manager, "_reconcile_container", reconcile_container)

    manager._ensure_container(HarnessKind.GROK, "token")  # pyright: ignore[reportPrivateUsage]

    assert seeded == [HarnessKind.GROK]


def test_registry_is_remote_for_every_kind() -> None:
    registry = build_remote_adapter_registry(SandboxManager(SandboxConfig.from_env({})))
    assert isinstance(registry, AdapterRegistry)
    for kind in HarnessKind:
        adapter = registry.create(kind)
        assert isinstance(adapter, RemoteHarnessAdapter)
        assert adapter.kind is kind
    # Fresh adapter per create call.
    first = registry.create(HarnessKind.CLAUDE)
    assert registry.create(HarnessKind.CLAUDE) is not first


def test_reconcile_recreates_container_when_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching but unstartable container is removed and created afresh.

    Docker Desktop restarts on WSL invalidate recorded bind-mount sources, so
    docker start fails forever; the manager must self-heal by recreating.
    """
    from docker.errors import DockerException

    events: list[str] = []

    class FakeContainer:
        status = "exited"

        def reload(self) -> None:
            events.append("reload")

        def start(self) -> None:
            events.append("start")
            raise DockerException("invalid mount config for type bind")

        def remove(self, force: bool = False) -> None:
            events.append(f"remove(force={force})")

    class Containers:
        def get(self, name: str) -> FakeContainer:
            return fake

        def run(self, image: str, **kwargs: Any) -> None:
            events.append(f"run:{image}")

    def always_matches(*args: Any, **kwargs: Any) -> bool:
        return True

    def mount_type(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    fake = FakeContainer()
    manager = SandboxManager(SandboxConfig.from_env({}))
    monkeypatch.setattr(manager, "_container_matches", always_matches)
    client: Any = SimpleNamespace(containers=Containers())
    manager._reconcile_container(  # pyright: ignore[reportPrivateUsage]
        client,
        mount_type,
        KeyError,
        kind=HarnessKind.GROK,
        name="tth-grok",
        image="tth-grok:latest",
        environment={"TTH_SPLIT_TOKEN": "tok"},
        token="tok",
    )

    assert events == [
        "reload",
        "reload",
        "start",
        "remove(force=True)",
        "run:tth-grok:latest",
    ]


@pytest.mark.parametrize(
    ("error_message", "reason"),
    [
        ("driver failed: port is already allocated", "port_conflict"),
        ("invalid mount config for type bind", "container_start_failed"),
    ],
)
def test_ensure_container_maps_run_failures_to_reasons(
    monkeypatch: pytest.MonkeyPatch,
    error_message: str,
    reason: str,
) -> None:
    from docker.errors import APIError

    def fake_docker_client(kind: HarnessKind) -> Any:
        return SimpleNamespace()

    manager = SandboxManager(SandboxConfig.from_env({}))
    monkeypatch.setattr(manager, "_docker_client", fake_docker_client)

    def failing_reconcile(*args: Any, **kwargs: Any) -> None:
        raise APIError(error_message)

    monkeypatch.setattr(manager, "_reconcile_container", failing_reconcile)
    _skip_rtk_seeding(monkeypatch)

    with pytest.raises(DomainError) as excinfo:
        manager._ensure_container(  # pyright: ignore[reportPrivateUsage]
            HarnessKind.GROK, "tok"
        )

    assert excinfo.value.code is ErrorCode.SANDBOX_UNAVAILABLE
    assert excinfo.value.details["reason"] == reason


def test_reconcile_recreates_container_whose_image_was_pruned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A running container whose image tag moved and whose old image is gone.

    Rebuilding ``tth-<kind>:latest`` untags the previous image and a prune (or
    buildx's own cleanup) can delete it; docker-py then raises NotFound while
    resolving ``container.image``. Seen after a sidecar rebuild: the proxy kept
    failing readiness with "No such image" instead of recreating the sandbox.
    """
    events: list[str] = []

    class Missing(Exception):
        pass

    class FakeContainer:
        status = "running"

        def reload(self) -> None:
            events.append("reload")

        def stop(self, timeout: int = 10) -> None:
            events.append("stop")

        def remove(self, force: bool = False) -> None:
            events.append(f"remove(force={force})")

    class Containers:
        def get(self, name: str) -> FakeContainer:
            return fake

        def run(self, image: str, **kwargs: Any) -> None:
            events.append(f"run:{image}")

    def image_lookup_fails(*args: Any, **kwargs: Any) -> bool:
        raise Missing("No such image")

    def mount_type(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    fake = FakeContainer()
    manager = SandboxManager(SandboxConfig.from_env({}))
    monkeypatch.setattr(manager, "_container_matches", image_lookup_fails)
    client: Any = SimpleNamespace(containers=Containers())
    manager._reconcile_container(  # pyright: ignore[reportPrivateUsage]
        client,
        mount_type,
        Missing,
        kind=HarnessKind.MUSE,
        name="tth-muse",
        image="tth-muse:latest",
        environment={"TTH_SPLIT_TOKEN": "tok"},
        token="tok",
    )

    assert events == ["reload", "stop", "remove(force=True)", "run:tth-muse:latest"]


# ---------------------------------------------------------------------------
# Workspace setup and toolchain environment
# ---------------------------------------------------------------------------


def test_environment_injects_toolchain_caches_and_passthrough_cannot_override_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UV_CACHE_DIR", "/operator/uv")
    config = SandboxConfig.from_env({"TTH_SANDBOX_ENV_GROK": "UV_CACHE_DIR,XAI_API_KEY"})
    manager = SandboxManager(config)

    environment = manager._environment(  # pyright: ignore[reportPrivateUsage]
        HarnessKind.GROK, "tok"
    )

    for name, value in TOOLCHAIN_ENV.items():
        assert environment[name] == value
    assert environment["UV_CACHE_DIR"] == "/data/uv/cache"
    # The image exports no uv settings of its own; only the caches are managed.
    assert "UV_PROJECT_ENVIRONMENT" not in environment
    assert "UV_PYTHON_DOWNLOADS" not in environment


def test_container_without_toolchain_env_is_recreated(tmp_path: Path) -> None:
    config = SandboxConfig(mount_roots=(str(tmp_path),))
    manager = SandboxManager(config)
    environment = manager._environment(  # pyright: ignore[reportPrivateUsage]
        HarnessKind.CODEX, "split"
    )
    env_lines = [f"{name}={value}" for name, value in environment.items()]
    container: Any = SimpleNamespace(
        image=SimpleNamespace(tags=["tth-codex:latest"]),
        attrs={
            "Config": {"Env": list(env_lines)},
            "HostConfig": {
                "PortBindings": {"8010/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8113"}]},
                "SecurityOpt": ["no-new-privileges:true", "seccomp=unconfined"],
                "PidsLimit": 512,
                "ExtraHosts": ["host.docker.internal:host-gateway"],
            },
            "Mounts": [
                {"Destination": "/home/agent", "Name": "tth-codex-home", "Type": "volume"},
                {"Destination": "/data", "Name": "tth-codex-data", "Type": "volume"},
                {"Destination": str(tmp_path), "Type": "bind"},
            ],
        },
    )

    def matches() -> bool:
        return manager._container_matches(  # pyright: ignore[reportPrivateUsage]
            container,
            HarnessKind.CODEX,
            image="tth-codex:latest",
            name="tth-codex",
            environment=environment,
        )

    assert matches()
    # A container from before the toolchain rollout carries none of the cache vars.
    container.attrs["Config"]["Env"] = [
        line for line in env_lines if not line.startswith("UV_PYTHON_INSTALL_DIR=")
    ]
    assert not matches()


def test_from_env_reads_workspace_setup_settings() -> None:
    default = SandboxConfig.from_env({})
    assert default.workspace_setup_enabled is True
    assert default.workspace_setup_timeout == 900.0

    tuned = SandboxConfig.from_env(
        {"TTH_WORKSPACE_SETUP": "0", "TTH_WORKSPACE_SETUP_TIMEOUT": "120"}
    )
    assert tuned.workspace_setup_enabled is False
    assert tuned.workspace_setup_timeout == 120.0


async def test_prepare_workspace_rejects_unmounted_paths_before_docker(tmp_path: Path) -> None:
    manager = SandboxManager(SandboxConfig(mount_roots=(str(tmp_path),)))

    with pytest.raises(DomainError) as excinfo:
        await manager.prepare_workspace(HarnessKind.CODEX, "/elsewhere/project")

    assert excinfo.value.code is ErrorCode.SANDBOX_PATH_NOT_MOUNTED


async def test_prepare_workspace_kill_switch_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manager = SandboxManager(
        SandboxConfig(mount_roots=(str(tmp_path),), workspace_setup_enabled=False)
    )

    def unexpected(*args: Any, **kwargs: Any) -> WorkspaceSetupOutcome:
        raise AssertionError("setup must not run when disabled")

    monkeypatch.setattr(manager, "_run_workspace_setup", unexpected)

    assert await manager.prepare_workspace(HarnessKind.CODEX, str(tmp_path / "p")) is None


async def test_prepare_workspace_serializes_per_kind_and_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manager = SandboxManager(SandboxConfig(mount_roots=(str(tmp_path),)))
    active = 0
    peak = 0
    started_events: list[WorkspaceSetupStarted] = []

    def run(
        kind: HarnessKind,
        working_directory: str,
        *,
        timeout: float,
        redaction_patterns: tuple[str, ...],
        on_started: Any,
    ) -> WorkspaceSetupOutcome:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        on_started(
            WorkspaceSetupStarted(
                working_directory=working_directory, setup_file=".tth/setup.sh", stamp="s"
            )
        )
        import time

        time.sleep(0.05)
        active -= 1
        assert timeout == 900.0
        assert redaction_patterns == ("hush",)
        return WorkspaceSetupOutcome(status="succeeded", working_directory=working_directory)

    monkeypatch.setattr(manager, "_run_workspace_setup", run)
    directory = str(tmp_path / "p")

    outcomes = await asyncio.gather(
        manager.prepare_workspace(
            HarnessKind.CODEX,
            directory,
            redaction_patterns=("hush",),
            on_started=started_events.append,
        ),
        manager.prepare_workspace(
            HarnessKind.CODEX,
            directory,
            redaction_patterns=("hush",),
            on_started=started_events.append,
        ),
    )

    assert peak == 1
    assert [outcome.status for outcome in outcomes if outcome is not None] == [
        "succeeded",
        "succeeded",
    ]
    # on_started is delivered on the event loop, not the worker thread.
    assert [event.working_directory for event in started_events] == [directory, directory]


async def test_prepare_workspace_maps_proxy_side_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manager = SandboxManager(
        SandboxConfig(mount_roots=(str(tmp_path),), workspace_setup_timeout=0.01)
    )
    monkeypatch.setattr("talktoharnesses.remote.sandbox._WORKSPACE_SETUP_GRACE", 0.01, raising=True)

    def hang(*args: Any, **kwargs: Any) -> WorkspaceSetupOutcome:
        import time

        time.sleep(0.5)
        return WorkspaceSetupOutcome(status="absent", working_directory="x")

    monkeypatch.setattr(manager, "_run_workspace_setup", hang)

    with pytest.raises(DomainError) as excinfo:
        await manager.prepare_workspace(HarnessKind.CODEX, str(tmp_path / "p"))

    assert excinfo.value.code is ErrorCode.WORKSPACE_SETUP_FAILED
    assert excinfo.value.details["reason"] == "timeout"


async def test_prepare_workspace_drops_progress_reported_after_it_returned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The docker exec thread outlives a timed-out await; its callback must not."""
    manager = SandboxManager(
        SandboxConfig(mount_roots=(str(tmp_path),), workspace_setup_timeout=0.01)
    )
    monkeypatch.setattr("talktoharnesses.remote.sandbox._WORKSPACE_SETUP_GRACE", 0.01, raising=True)
    started_events: list[WorkspaceSetupStarted] = []
    finished = threading.Event()

    def late_start(*args: Any, on_started: Any, **kwargs: Any) -> WorkspaceSetupOutcome:
        time.sleep(0.1)
        on_started(WorkspaceSetupStarted(working_directory="x", setup_file="s", stamp="late"))
        finished.set()
        return WorkspaceSetupOutcome(status="succeeded", working_directory="x")

    monkeypatch.setattr(manager, "_run_workspace_setup", late_start)

    with pytest.raises(DomainError) as excinfo:
        await manager.prepare_workspace(
            HarnessKind.CODEX, str(tmp_path / "p"), on_started=started_events.append
        )
    assert excinfo.value.details["reason"] == "timeout"

    await asyncio.to_thread(finished.wait, 2)
    await asyncio.sleep(0.05)
    assert started_events == []
