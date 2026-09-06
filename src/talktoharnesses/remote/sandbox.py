"""Docker sandbox lifecycle for split services.

The proxy owns one long-lived container per harness kind, spawned on demand
the first time a kind's endpoint is resolved. A missing image is built locally
from the repo's per-kind build context. Containers are health-checked before
an endpoint is handed out, reused across client requests, recorded in the
sandbox store so the proxy can reattach to them after a restart, and left
running when the proxy shuts down.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import httpx
from pydantic import BaseModel
from tth_types.base import FROZEN
from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError

from talktoharnesses.remote import docker_ops, sandbox_auth
from talktoharnesses.remote.docker_ops import HOST_GATEWAY_ALIAS as _HOST_GATEWAY_ALIAS
from talktoharnesses.remote.docker_ops import container_otlp_endpoint as _container_otlp_endpoint
from talktoharnesses.remote.docker_ops import (
    ensure_docker_cli_available as ensure_docker_cli_available,
)
from talktoharnesses.remote.docker_ops import kind_slug as _kind_slug
from talktoharnesses.remote.docker_ops import rewrite_loopback_url as rewrite_loopback_url
from talktoharnesses.remote.sandbox_auth import AuthFileSpec as AuthFileSpec

logger = logging.getLogger(__name__)

DEFAULT_PORTS: dict[HarnessKind, int] = {
    HarnessKind.GROK: 8111,
    HarnessKind.CURSOR: 8112,
    HarnessKind.CODEX: 8113,
    HarnessKind.CLAUDE: 8114,
    HarnessKind.OPENCODE: 8115,
    HarnessKind.PRIME_AGENT: 8116,
    HarnessKind.MUSE: 8117,
}

# Provider credential env vars forwarded into each kind's container when set.
DEFAULT_ENV_PASSTHROUGH: dict[HarnessKind, tuple[str, ...]] = {
    HarnessKind.GROK: ("XAI_API_KEY",),
    HarnessKind.CURSOR: ("CURSOR_API_KEY",),
    HarnessKind.CODEX: ("OPENAI_API_KEY",),
    HarnessKind.CLAUDE: ("ANTHROPIC_API_KEY",),
    HarnessKind.OPENCODE: ("OPENCODE_API_KEY",),
    HarnessKind.PRIME_AGENT: (),
    HarnessKind.MUSE: ("META_API_KEY",),
}

_OTEL_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
_OTEL_HEADERS_ENV = "OTEL_EXPORTER_OTLP_HEADERS"
# Container env the manager owns; operator passthrough may never override it.
_MANAGED_ENV_KEYS = frozenset({"TTH_SPLIT_TOKEN", _OTEL_ENDPOINT_ENV, _OTEL_HEADERS_ENV})


_CONTAINER_PORT = 8010


def _split_port_env(kind: HarnessKind) -> str:
    return f"TTH_SPLIT_PORT_{kind.value.upper()}"


def _security_options(kind: HarnessKind) -> list[str]:
    options = ["no-new-privileges:true"]
    if kind is HarnessKind.CODEX:
        # Codex runs its own nested sandbox (Landlock + seccomp), and
        # installing those syscall filters is blocked by Docker's default
        # seccomp profile. Only the seccomp layer is relaxed, only for this
        # kind; no-new-privileges, cap_drop=ALL, and the pids/memory limits
        # still apply.
        options.append("seccomp=unconfined")
    return options


def _pids_limit(kind: HarnessKind) -> int:
    if kind is HarnessKind.GROK:
        # grok is a multi-threaded Rust binary; around ten concurrent sessions
        # exhaust 512 pids and panic with "OS can't spawn worker thread".
        return 2048
    return 512


class SandboxConfig(BaseModel):
    model_config = FROZEN

    ports: dict[HarnessKind, int] = {}
    image_tag: str = "latest"
    # Host roots bind-mounted into every sandbox container at the same path.
    # Set via TTH_SANDBOX_MOUNT_ROOTS (colon-separated absolute paths),
    # defaulting to $HOME/dev; harness working directories and workspace
    # roots must live under one of them.
    mount_roots: tuple[str, ...] = ()
    env_passthrough: dict[HarnessKind, tuple[str, ...]] = {}
    auth_files: dict[HarnessKind, str] = {}
    forward_otel_headers: bool = False
    health_timeout: float = 90.0
    health_poll_interval: float = 1.0
    build_timeout: float = 1800.0
    prepare_grace: float = 60.0

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> SandboxConfig:
        env = dict(os.environ) if environ is None else environ
        ports: dict[HarnessKind, int] = dict(DEFAULT_PORTS)
        for kind in HarnessKind:
            port = env.get(_split_port_env(kind))
            if port:
                ports[kind] = int(port)
        passthrough = dict(DEFAULT_ENV_PASSTHROUGH)
        for kind in HarnessKind:
            raw = env.get(f"TTH_SANDBOX_ENV_{kind.value.upper()}")
            if raw is not None:
                passthrough[kind] = tuple(name.strip() for name in raw.split(",") if name.strip())
        auth_files = sandbox_auth.auth_files_from_env(env)
        home = env.get("HOME")
        raw_mount_roots = env.get("TTH_SANDBOX_MOUNT_ROOTS")
        if raw_mount_roots is not None:
            mount_roots = tuple(root.strip() for root in raw_mount_roots.split(":") if root.strip())
        else:
            mount_roots = (str(Path(home) / "dev"),) if home else ()
        return cls(
            ports=ports,
            image_tag=env.get("TTH_SANDBOX_IMAGE_TAG", "latest"),
            mount_roots=mount_roots,
            env_passthrough=passthrough,
            auth_files=auth_files,
            forward_otel_headers=env.get("TTH_SANDBOX_FORWARD_OTEL_HEADERS") == "1",
        )


@dataclass(frozen=True)
class SplitEndpoint:
    base_url: str
    token: str | None = None
    # Set for proxy-managed sandboxes: the hostname a container uses to reach
    # loopback services on the proxy host, so harness-facing URLs in the
    # configuration (MCP servers) can be rewritten before they cross over.
    loopback_alias: str | None = None


class SandboxRecordData(BaseModel):
    """One sandbox the proxy has spawned, as persisted in the sandbox store."""

    kind: HarnessKind
    container_name: str
    image: str
    host_port: int
    base_url: str
    split_token: str
    status: str  # "preparing" | "ready" | "failed"
    created_at: datetime
    updated_at: datetime
    last_ready_at: datetime | None = None


class SandboxStore(Protocol):
    """Persistence for sandbox records; implemented by the Django layer."""

    async def get(self, kind: HarnessKind) -> SandboxRecordData | None: ...

    async def upsert(self, record: SandboxRecordData) -> None: ...

    async def reserve(self, record: SandboxRecordData) -> SandboxRecordData:
        """Atomically insert ``record`` unless the kind already has a row.

        Returns the stored record either way. Concurrent workers preparing the
        same kind must all end up with the first writer's split token; a plain
        get-then-upsert would let each mint its own and recreate each other's
        container.
        """
        ...


@dataclass
class SandboxManager:
    """Ensures a healthy split endpoint per kind; boots Docker sandboxes on demand."""

    config: SandboxConfig
    store: SandboxStore | None = None
    _locks: dict[HarnessKind, asyncio.Lock] = field(default_factory=dict[HarnessKind, asyncio.Lock])
    _endpoints: dict[HarnessKind, SplitEndpoint] = field(
        default_factory=dict[HarnessKind, SplitEndpoint]
    )
    _prepare_tasks: dict[HarnessKind, asyncio.Task[SplitEndpoint]] = field(
        default_factory=dict[HarnessKind, asyncio.Task[SplitEndpoint]]
    )
    _auth_signatures: dict[HarnessKind, sandbox_auth.AuthSignature | None] = field(
        default_factory=dict[HarnessKind, sandbox_auth.AuthSignature | None]
    )

    def _lock_for(self, kind: HarnessKind) -> asyncio.Lock:
        lock = self._locks.get(kind)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[kind] = lock
        return lock

    def _port_for(self, kind: HarnessKind) -> int:
        return self.config.ports.get(kind, DEFAULT_PORTS[kind])

    def _mount_roots(self) -> tuple[str, ...]:
        """Host roots bind-mounted into every sandbox (patched by live gates)."""
        return self.config.mount_roots

    def _require_mounted(self, kind: HarnessKind, required_paths: tuple[str, ...]) -> None:
        """Reject paths a sandbox cannot see before any Docker work happens."""
        roots = tuple(Path(root) for root in self._mount_roots())
        for raw in required_paths:
            path = Path(raw)
            if any(path.is_relative_to(root) for root in roots):
                continue
            raise DomainError(
                ErrorCode.SANDBOX_PATH_NOT_MOUNTED,
                f"{raw} is not mounted into the {kind.value} sandbox",
                details={
                    "kind": kind.value,
                    "path": raw,
                    "mount_roots": [str(root) for root in roots],
                    "reason": "path_not_mounted",
                },
            )

    async def endpoint(
        self,
        kind: HarnessKind,
        required_paths: tuple[str, ...] = (),
    ) -> SplitEndpoint:
        self._require_mounted(kind, required_paths)
        async with self._lock_for(kind):
            cached = self._endpoints.get(kind)
            if cached is not None:
                auth_changed = self._auth_signatures.get(kind) != self._auth_signature(kind)
                if not auth_changed and await self._endpoint_healthy(kind, cached):
                    return cached
                self._endpoints.pop(kind, None)
            task = self._prepare_tasks.get(kind)
            if task is None or task.done():
                task = asyncio.create_task(self._prepare(kind))
                self._prepare_tasks[kind] = task
        # Shield the shared prepare task: a caller timing out (or being
        # cancelled) must not abort an image build other callers will join.
        try:
            return await asyncio.wait_for(asyncio.shield(task), self.config.prepare_grace)
        except TimeoutError:
            raise DomainError(
                ErrorCode.SANDBOX_PREPARING,
                f"sandbox for {kind.value} is being prepared",
                details={"kind": kind.value},
            ) from None

    async def _prepare(self, kind: HarnessKind) -> SplitEndpoint:
        name = self._container_name(kind)
        image = self._image(kind)
        port = self._port_for(kind)
        base_url = f"http://127.0.0.1:{port}"
        now = datetime.now(UTC)
        record = SandboxRecordData(
            kind=kind,
            container_name=name,
            image=image,
            host_port=port,
            base_url=base_url,
            split_token=secrets.token_urlsafe(32),
            status="preparing",
            created_at=now,
            updated_at=now,
            last_ready_at=None,
        )
        if self.store is not None:
            # Atomic claim: whichever worker inserts the kind's row first owns
            # the split token, and everyone else adopts it.
            stored = await self.store.reserve(record)
            record = record.model_copy(
                update={
                    "split_token": stored.split_token,
                    "created_at": stored.created_at,
                    "last_ready_at": stored.last_ready_at,
                }
            )
        token = record.split_token
        await self._save(record)
        try:
            await asyncio.to_thread(self._ensure_image, kind)
            await asyncio.to_thread(self._ensure_container, kind, token)
            await self._wait_healthy(kind, base_url)
        except BaseException:
            await self._save(record.model_copy(update={"status": "failed"}))
            raise
        ready_at = datetime.now(UTC)
        await self._save(record.model_copy(update={"status": "ready", "last_ready_at": ready_at}))
        endpoint = SplitEndpoint(base_url=base_url, token=token, loopback_alias=_HOST_GATEWAY_ALIAS)
        self._endpoints[kind] = endpoint
        self._auth_signatures[kind] = self._auth_signature(kind)
        return endpoint

    @staticmethod
    def _health_kind(response: httpx.Response) -> object:
        """The kind reported by a /v1/health body, or None when unparseable."""
        try:
            body_obj: object = response.json()
        except ValueError:
            return None
        if not isinstance(body_obj, dict):
            return None
        return cast(dict[str, object], body_obj).get("kind")

    async def _endpoint_healthy(self, kind: HarnessKind, endpoint: SplitEndpoint) -> bool:
        try:
            async with httpx.AsyncClient(base_url=endpoint.base_url, timeout=2.0) as client:
                response = await client.get("/v1/health")
            return response.status_code == 200 and self._health_kind(response) == kind.value
        except httpx.HTTPError:
            return False

    async def _save(self, record: SandboxRecordData) -> None:
        if self.store is None:
            return
        await self.store.upsert(record.model_copy(update={"updated_at": datetime.now(UTC)}))

    async def is_running(self, kind: HarnessKind) -> bool:
        """True when the kind's sandbox is up, without spawning or building.

        Used as the readiness monitor's spawn gate: background probing may
        reattach to a container that is already running, but must never create
        containers or build images.
        """
        record = await self.store.get(kind) if self.store is not None else None
        if record is None and kind not in self._endpoints:
            return False
        name = record.container_name if record is not None else self._container_name(kind)
        return await asyncio.to_thread(self._container_running, name)

    def _container_running(self, container_name: str) -> bool:
        return docker_ops.container_running(container_name)

    def _container_name(self, kind: HarnessKind) -> str:
        return f"tth-{_kind_slug(kind)}"

    def _image(self, kind: HarnessKind) -> str:
        return f"{self._container_name(kind)}:{self.config.image_tag}"

    def _environment(self, kind: HarnessKind, token: str) -> dict[str, str]:
        # OTel vars are always managed (not via the per-kind passthrough
        # tuples) so TTH_SANDBOX_ENV_<KIND> overrides cannot drop them.
        # OTEL_SERVICE_NAME is deliberately not forwarded: each split bakes
        # its own service name.
        environment = {
            "TTH_SPLIT_TOKEN": token,
            _OTEL_ENDPOINT_ENV: _container_otlp_endpoint(os.environ.get(_OTEL_ENDPOINT_ENV)),
        }
        headers = os.environ.get(_OTEL_HEADERS_ENV)
        if headers and self.config.forward_otel_headers:
            environment[_OTEL_HEADERS_ENV] = headers
        for env_name in self.config.env_passthrough.get(kind, ()):
            # Managed keys are never passthrough: a host TTH_SPLIT_TOKEN
            # forwarded here would make the container disagree with the
            # stored record and 401 every proxy call.
            if env_name in _MANAGED_ENV_KEYS:
                continue
            value = os.environ.get(env_name)
            if value:
                environment[env_name] = value
        return environment

    def _container_matches(
        self,
        container: Any,
        kind: HarnessKind,
        *,
        image: str,
        name: str,
        environment: dict[str, str],
    ) -> bool:
        tags = list(getattr(container.image, "tags", []) or [])
        if image not in tags:
            return False

        attrs: dict[str, Any] = container.attrs
        actual_environment = {
            item.split("=", 1)[0]: item.split("=", 1)[1]
            for item in attrs.get("Config", {}).get("Env", [])
            if "=" in item
        }
        managed_environment = {
            "TTH_SPLIT_TOKEN",
            _OTEL_ENDPOINT_ENV,
            _OTEL_HEADERS_ENV,
            *self.config.env_passthrough.get(kind, ()),
        }
        if any(
            actual_environment.get(env_name) != environment.get(env_name)
            for env_name in managed_environment
        ):
            return False

        bindings = cast(
            object,
            attrs.get("HostConfig", {}).get("PortBindings", {}).get(f"{_CONTAINER_PORT}/tcp"),
        )
        if not isinstance(bindings, list) or not bindings:
            return False
        first_binding = cast(object, bindings[0])
        if not isinstance(first_binding, dict):
            return False
        binding = cast(dict[str, Any], first_binding)
        if binding.get("HostIp") != "127.0.0.1" or binding.get("HostPort") != str(
            self._port_for(kind)
        ):
            return False

        security_options = attrs.get("HostConfig", {}).get("SecurityOpt", [])
        if set(security_options) != set(_security_options(kind)):
            return False
        if attrs.get("HostConfig", {}).get("PidsLimit") != _pids_limit(kind):
            return False

        extra_hosts = cast("list[str]", attrs.get("HostConfig", {}).get("ExtraHosts") or [])
        if f"{_HOST_GATEWAY_ALIAS}:host-gateway" not in extra_hosts:
            return False

        actual_mounts = {mount.get("Destination"): mount for mount in attrs.get("Mounts", [])}
        expected_volumes = {
            "/home/agent": f"{name}-home",
            "/data": f"{name}-data",
        }
        for target, volume_name in expected_volumes.items():
            mount = actual_mounts.get(target)
            if mount is None or mount.get("Type") != "volume" or mount.get("Name") != volume_name:
                return False
        for root in self._mount_roots():
            mount = actual_mounts.get(str(Path(root)))
            if mount is None or mount.get("Type") != "bind":
                return False
        return True

    def _auth_file(self, kind: HarnessKind) -> str | None:
        return self.config.auth_files.get(kind)

    def _auth_signature(self, kind: HarnessKind) -> sandbox_auth.AuthSignature | None:
        return sandbox_auth.auth_signature(self._auth_file(kind))

    def _seed_auth_file(
        self,
        client: Any,
        mount_type: Any,
        *,
        kind: HarnessKind,
        image: str,
        name: str,
    ) -> None:
        auth_file = self._auth_file(kind)
        if auth_file is None:
            return
        sandbox_auth.seed_auth_file(
            client,
            mount_type,
            kind=kind,
            auth_file=auth_file,
            image=image,
            home_volume=f"{name}-home",
        )

    def _docker_client(self, kind: HarnessKind) -> Any:
        return docker_ops.docker_client(kind)

    def _ensure_image(self, kind: HarnessKind) -> None:
        """Blocking; builds the split image locally when it is missing."""
        client = self._docker_client(kind)
        from docker.errors import DockerException, ImageNotFound

        image = self._image(kind)
        try:
            client.images.get(image)
        except ImageNotFound:
            logger.info("sandbox image %s not found locally; building it", image)
            self._build_image(kind)
        except DockerException as exc:
            logger.warning("docker image inspection failed for %s: %s", image, exc)
            raise DomainError(
                ErrorCode.SANDBOX_UNAVAILABLE,
                f"docker image inspection failed: {exc}",
                details={"kind": kind.value, "reason": "docker_unavailable"},
            ) from exc

    def _resolve_build_root(self, kind: HarnessKind) -> Path:
        return docker_ops.resolve_build_root(kind)

    def _build_image(self, kind: HarnessKind) -> None:
        """Blocking; builds the split image from the repo's per-kind context."""
        root = self._resolve_build_root(kind)
        docker_ops.build_image(
            kind, self._image(kind), root=root, timeout=self.config.build_timeout
        )

    def _ensure_container(self, kind: HarnessKind, token: str) -> None:
        """Blocking docker-py path; always called via asyncio.to_thread."""
        client = self._docker_client(kind)
        from docker.errors import APIError, DockerException, NotFound
        from docker.types import Mount

        name = self._container_name(kind)
        image = self._image(kind)
        environment = self._environment(kind, token)
        try:
            # Refresh the managed volume even when the running container still
            # matches; host logins can rotate while the sandbox stays alive.
            self._seed_auth_file(client, Mount, kind=kind, image=image, name=name)
            self._reconcile_container(
                client,
                Mount,
                NotFound,
                kind=kind,
                name=name,
                image=image,
                environment=environment,
                token=token,
            )
        except DomainError:
            raise
        except APIError as exc:
            logger.warning("docker failed to run sandbox %s: %s", name, exc)
            message = str(exc).lower()
            port_conflict = (
                "port is already allocated" in message or "address already in use" in message
            )
            raise DomainError(
                ErrorCode.SANDBOX_UNAVAILABLE,
                f"docker failed to run sandbox {name}: {exc}",
                details={
                    "kind": kind.value,
                    "reason": "port_conflict" if port_conflict else "container_start_failed",
                },
            ) from exc
        except DockerException as exc:
            logger.warning("docker failed to run sandbox %s: %s", name, exc)
            raise DomainError(
                ErrorCode.SANDBOX_UNAVAILABLE,
                f"docker failed to run sandbox {name}: {exc}",
                details={"kind": kind.value, "reason": "container_start_failed"},
            ) from exc

    def _reconcile_container(
        self,
        client: Any,
        mount_type: Any,
        not_found: type[Exception],
        *,
        kind: HarnessKind,
        name: str,
        image: str,
        environment: dict[str, str],
        token: str,
    ) -> None:
        try:
            container = client.containers.get(name)
        except not_found:
            container = None

        if container is not None:
            container.reload()
            if container.status == "dead" or not self._container_matches(
                container,
                kind,
                image=image,
                name=name,
                environment=environment,
            ):
                logger.info("recreating sandbox %s for managed configuration drift", name)
                if container.status not in {"dead", "exited"}:
                    container.stop(timeout=10)
                container.remove(force=True)
                container = None

        if container is None:
            self._create_container(
                client, mount_type, kind=kind, name=name, image=image, environment=environment
            )
            return

        container.reload()
        # A "restarting" container is left to Docker's restart policy; the
        # health wait below decides whether it comes back in time.
        if container.status in {"running", "restarting"}:
            return
        from docker.errors import DockerException

        try:
            container.start()
        except DockerException as exc:
            # A stopped container can become unstartable, e.g. Docker Desktop
            # restarts invalidate recorded bind-mount sources on WSL.
            logger.warning("sandbox %s failed to start; recreating it: %s", name, exc)
            container.remove(force=True)
            self._create_container(
                client, mount_type, kind=kind, name=name, image=image, environment=environment
            )

    def _create_container(
        self,
        client: Any,
        mount_type: Any,
        *,
        kind: HarnessKind,
        name: str,
        image: str,
        environment: dict[str, str],
    ) -> None:
        mounts: list[Any] = [
            mount_type(target="/home/agent", source=f"{name}-home", type="volume"),
            mount_type(target="/data", source=f"{name}-data", type="volume"),
        ]
        for root in self._mount_roots():
            # Identical container path: harness configs and git worktrees
            # embed absolute host paths.
            host_path = str(Path(root))
            mounts.append(mount_type(target=host_path, source=host_path, type="bind"))
        client.containers.run(
            image,
            name=name,
            detach=True,
            init=True,
            restart_policy={"Name": "unless-stopped"},
            ports={f"{_CONTAINER_PORT}/tcp": ("127.0.0.1", self._port_for(kind))},
            mounts=mounts,
            environment=environment,
            # Lets containers reach a collector on the host at the
            # host.docker.internal endpoint _environment injects.
            extra_hosts={_HOST_GATEWAY_ALIAS: "host-gateway"},
            cap_drop=["ALL"],
            security_opt=_security_options(kind),
            pids_limit=_pids_limit(kind),
            mem_limit="4g",
        )

    async def _wait_healthy(self, kind: HarnessKind, base_url: str) -> None:
        deadline = asyncio.get_running_loop().time() + self.config.health_timeout
        last_error: str | None = None
        async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    response = await client.get("/v1/health")
                except httpx.HTTPError as exc:
                    last_error = str(exc)
                else:
                    if response.status_code == 200:
                        # Same contract as _endpoint_healthy: a 200 from a
                        # leftover process (or the wrong split) on the
                        # published port must not mark this kind ready.
                        reported = self._health_kind(response)
                        if reported == kind.value:
                            return
                        last_error = f"health kind mismatch: {reported!r}"
                    else:
                        last_error = f"HTTP {response.status_code}"
                await asyncio.sleep(self.config.health_poll_interval)
        logger.warning(
            "split sandbox for %s did not become healthy at %s: %s",
            kind.value,
            base_url,
            last_error or "timeout",
        )
        raise DomainError(
            ErrorCode.SANDBOX_UNAVAILABLE,
            f"split sandbox for {kind.value} did not become healthy",
            details={
                "kind": kind.value,
                "reason": "health_timeout",
                "last_error": last_error or "timeout",
            },
        )

    async def shutdown(self) -> None:
        """Containers deliberately stay running; nothing to release."""
        return
