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
from collections.abc import Mapping, Sequence
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


def base_image(kind: HarnessKind, tag: str) -> str:
    """The kind's harness image, as ``deploy/build-splits.sh`` tags it."""
    return f"tth-{kind_slug(kind)}:{tag}"


def image_environment(config: Mapping[str, Any] | None) -> dict[str, str]:
    """The ``Env`` of an image or container configuration, as a mapping."""
    items: list[str] = (config or {}).get("Env") or []
    return dict(item.split("=", 1) for item in items if "=" in item)


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


def docker_client(kind: HarnessKind | None = None) -> Any:
    """Blocking docker-py client factory with actionable failures.

    ``kind`` names the sandbox that needs Docker in the failure details;
    scope-wide work such as reclaiming has none.
    """
    details: dict[str, str] = {"reason": "docker_unavailable"}
    if kind is not None:
        details["kind"] = kind.value
    try:
        import docker
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise DomainError(
            ErrorCode.SANDBOX_UNAVAILABLE, "docker SDK is not installed", details=details
        ) from exc
    from docker.errors import DockerException

    try:
        return docker.from_env()
    except DockerException as exc:
        logger.warning("docker daemon is unreachable (%s): %s", details.get("kind", "scope"), exc)
        raise DomainError(
            ErrorCode.SANDBOX_UNAVAILABLE,
            f"docker daemon is unreachable: {exc}",
            details=details,
        ) from exc


def docker_failure(exc: Exception, *, name: str, kind: HarnessKind) -> DomainError:
    """Classify a docker-py failure while preparing sandbox ``name``.

    The daemon's text stays in the internal message and the log; clients get
    only the fixed wording of the ``reason``.
    """
    from docker.errors import APIError

    logger.warning("docker failed to prepare sandbox %s: %s", name, exc)
    reason = "container_start_failed"
    if isinstance(exc, APIError):
        message = str(exc).lower()
        if "port is already allocated" in message or "address already in use" in message:
            reason = "port_conflict"
        elif "all predefined address pools have been fully subnetted" in message:
            reason = "network_pool_exhausted"
    return DomainError(
        ErrorCode.SANDBOX_UNAVAILABLE,
        f"docker failed to prepare sandbox {name}: {exc}",
        details={"kind": kind.value, "reason": reason},
    )


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
    required = (
        root / "docker-bake.hcl",
        root / f"tth-{kind_slug(kind)}" / "Dockerfile",
        root / "tth-types" / "pyproject.toml",
    )
    if not all(path.is_file() for path in required):
        raise DomainError(
            ErrorCode.SANDBOX_UNAVAILABLE,
            f"no build context for {kind.value} under {root}",
            details={"kind": kind.value, "reason": "build_context_missing"},
        )
    return root


def build_image(kind: HarnessKind, image: str, *, root: Path, timeout: float) -> None:
    """Blocking build of one kind's bake target (the recipe lives in docker-bake.hcl).

    buildx is required: the split Dockerfiles consume the shared tth-types
    sources through a named build context, which docker-py cannot express.
    Bake runs from ``root`` because it only reads contexts below the working
    directory.
    """
    docker_bin = ensure_docker_cli_available(kind)
    slug = kind_slug(kind)
    command = [
        docker_bin,
        "buildx",
        "bake",
        "-f",
        "docker-bake.hcl",
        "--load",
        "--set",
        f"{slug}.tags={image}",
        slug,
    ]
    env = {**os.environ, "HOST_UID": str(os.getuid()), "HOST_GID": str(os.getgid())}
    run_image_build(command, kind=kind, image=image, cwd=root, timeout=timeout, env=env)


def docker_driver_builder(docker_bin: str, *, kind: HarnessKind, reason: str) -> str:
    """Blocking; the buildx builder that builds into the daemon's own image store.

    It is named after the current Docker context. Only that builder resolves
    ``FROM`` against locally built images and needs no ``--load``; the builder
    an operator selected may run elsewhere.
    """
    try:
        result = subprocess.run(
            [docker_bin, "context", "show"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DomainError(
            ErrorCode.SANDBOX_UNAVAILABLE,
            "docker context show timed out",
            details={"kind": kind.value, "reason": reason},
        ) from exc
    context = result.stdout.strip()
    if result.returncode != 0 or not context:
        logger.error("docker context show failed: %s", result.stderr.strip())
        raise DomainError(
            ErrorCode.SANDBOX_UNAVAILABLE,
            "could not determine the current docker context",
            details={"kind": kind.value, "reason": reason},
        )
    return context


def run_image_build(
    command: Sequence[str],
    *,
    kind: HarnessKind,
    image: str,
    cwd: Path,
    timeout: float,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
    reason: str = "image_build_failed",
) -> None:
    """Blocking; run a docker CLI build of ``image``, logging its output on failure.

    ``stdin`` is fed to the command (a Dockerfile for ``docker build -``).
    Failures become ``sandbox_unavailable`` with ``reason``; the output's tail
    goes into the details, never into the message.
    """
    logger.info("building sandbox image %s", image)
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            env=env,
            input=stdin,
            capture_output=True,
            # Build output is whatever the instructions print, not always UTF-8.
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DomainError(
            ErrorCode.SANDBOX_UNAVAILABLE,
            f"sandbox image build for {image} timed out",
            details={"kind": kind.value, "reason": reason},
        ) from exc
    if result.returncode != 0:
        output = f"{result.stdout}\n{result.stderr}".strip()
        logger.error("sandbox image build failed for %s:\n%s", image, output)
        tail = "\n".join(output.splitlines()[-20:])
        raise DomainError(
            ErrorCode.SANDBOX_UNAVAILABLE,
            f"sandbox image build failed for {image}",
            details={
                "kind": kind.value,
                "reason": reason,
                "build_tail": tail,
            },
        )
    logger.info("built sandbox image %s", image)


SANDBOX_HOME = "/home/agent"


def run_home_seeder(
    client: Any,
    mount_type: Any,
    *,
    image: str,
    home_volume: str,
    command: Sequence[str],
    extra_mounts: Sequence[Any] = (),
) -> None:
    """Run ``command`` in a hardened one-shot container against a kind's home volume.

    The container runs the split image with networking disabled, every
    capability dropped, and no privilege escalation; it is removed on exit.
    Callers say what to run, this says how.
    """
    client.containers.run(
        image,
        command=list(command),
        environment={"HOME": SANDBOX_HOME},
        mounts=[
            *extra_mounts,
            mount_type(target=SANDBOX_HOME, source=home_volume, type="volume"),
        ],
        network_disabled=True,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        remove=True,
    )
