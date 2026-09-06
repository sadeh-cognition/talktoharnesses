"""Blocking Docker mechanics used by the sandbox manager.

Everything here is synchronous and talks to the docker CLI or docker-py; the
manager runs it via ``asyncio.to_thread``. Nothing here knows about split
tokens, sandbox records, or health polling.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError

logger = logging.getLogger(__name__)

HOST_GATEWAY_ALIAS = "host.docker.internal"
_LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})
_OTEL_OPT_OUT_VALUES = frozenset({"false", "0"})


def kind_slug(kind: HarnessKind) -> str:
    return kind.value.replace("_", "-")


def rewrite_loopback_url(url: str, alias: str) -> str:
    """Point a loopback URL at ``alias`` so a container reaches the proxy host.

    Scheme, port, path, and query are preserved; non-loopback hosts pass
    through unchanged.
    """
    parts = urlsplit(url)
    if parts.hostname and parts.hostname.lower() in _LOCAL_HOSTNAMES:
        netloc = alias if parts.port is None else f"{alias}:{parts.port}"
        return urlunsplit(parts._replace(netloc=netloc))
    return url


def container_otlp_endpoint(raw: str | None) -> str:
    """Map the host-side OTLP endpoint to the value a sandbox should see.

    Unset/empty resolves to the host-gateway default; localhost endpoints are
    rewritten to host.docker.internal preserving scheme, port, and path; the
    false/0 opt-out sentinel passes through verbatim so splits disable
    themselves too; remote endpoints pass through unchanged.
    """
    if raw is None or not raw.strip():
        return f"http://{HOST_GATEWAY_ALIAS}:4318"
    value = raw.strip()
    if value.lower() in _OTEL_OPT_OUT_VALUES:
        return value
    return rewrite_loopback_url(value, HOST_GATEWAY_ALIAS)


def ensure_docker_cli_available(kind: HarnessKind | None = None) -> str:
    """Return the docker CLI path, failing closed when it is not installed.

    Every harness runs in a Docker sandbox, so the backend calls this at
    startup to refuse to serve without the CLI; image builds call it again
    with the kind for an actionable per-kind error.
    """
    docker_bin = shutil.which("docker")
    if docker_bin is None:
        details: dict[str, str] = {"reason": "docker_unavailable"}
        if kind is not None:
            details["kind"] = kind.value
        raise DomainError(
            ErrorCode.SANDBOX_UNAVAILABLE,
            "docker CLI is not installed",
            details=details,
        )
    return docker_bin


def docker_client(kind: HarnessKind) -> Any:
    """Blocking docker-py client factory with actionable failures."""
    try:
        import docker
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise DomainError(
            ErrorCode.SANDBOX_UNAVAILABLE,
            "docker SDK is not installed",
            details={"kind": kind.value, "reason": "docker_unavailable"},
        ) from exc
    from docker.errors import DockerException

    try:
        return docker.from_env()
    except DockerException as exc:
        logger.warning("docker daemon is unreachable for %s sandbox: %s", kind.value, exc)
        raise DomainError(
            ErrorCode.SANDBOX_UNAVAILABLE,
            f"docker daemon is unreachable: {exc}",
            details={"kind": kind.value, "reason": "docker_unavailable"},
        ) from exc


def container_running(container_name: str) -> bool:
    """True when docker reports the named container as running; False on any error."""
    try:
        import docker
    except ImportError:
        return False
    try:
        client: Any = docker.from_env()
        container = client.containers.get(container_name)
    except Exception:
        return False
    return getattr(container, "status", None) == "running"


def resolve_build_root(kind: HarnessKind) -> Path:
    """Locate the repo root holding the per-kind build contexts.

    Works for editable installs (the package sits at <root>/src/...); wheel
    installs have no build contexts on disk and must pre-build images.
    """
    import talktoharnesses

    root = Path(talktoharnesses.__file__).resolve().parents[2]
    dockerfile = root / f"tth-{kind_slug(kind)}" / "Dockerfile"
    tth_types = root / "tth-types" / "pyproject.toml"
    if not dockerfile.is_file() or not tth_types.is_file():
        raise DomainError(
            ErrorCode.SANDBOX_UNAVAILABLE,
            f"no build context for {kind.value} under {root}",
            details={"kind": kind.value, "reason": "build_context_missing"},
        )
    return root


def build_image(kind: HarnessKind, image: str, *, root: Path, timeout: float) -> None:
    """Blocking replication of deploy/build-splits.sh for one kind.

    buildx is required: the split Dockerfiles consume the shared tth-types
    sources through a named build context, which docker-py cannot express.
    """
    docker_bin = ensure_docker_cli_available(kind)
    command = [
        docker_bin,
        "buildx",
        "build",
        "--build-context",
        f"tth_types={root / 'tth-types'}",
        "--build-arg",
        f"UID={os.getuid()}",
        "--build-arg",
        f"GID={os.getgid()}",
        "--load",
        "-t",
        image,
        str(root / f"tth-{kind_slug(kind)}"),
    ]
    logger.info("building sandbox image %s", image)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DomainError(
            ErrorCode.SANDBOX_UNAVAILABLE,
            f"sandbox image build for {kind.value} timed out",
            details={"kind": kind.value, "reason": "image_build_failed"},
        ) from exc
    if result.returncode != 0:
        output = f"{result.stdout}\n{result.stderr}".strip()
        logger.error("sandbox image build failed for %s:\n%s", image, output)
        tail = "\n".join(output.splitlines()[-20:])
        raise DomainError(
            ErrorCode.SANDBOX_UNAVAILABLE,
            f"sandbox image build failed for {kind.value}",
            details={
                "kind": kind.value,
                "reason": "image_build_failed",
                "build_tail": tail,
            },
        )
    logger.info("built sandbox image %s", image)
