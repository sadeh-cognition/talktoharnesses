"""Per-kind host credential files and how they are seeded into a sandbox.

Sandbox lifecycle may contain narrowly scoped credential setup: each kind's
host credential file is copied into the managed home volume at container
creation (and re-seeded when the host file changes). This module owns the
per-kind file locations and the one-shot seeding container; it knows nothing
about the split process or protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError


@dataclass(frozen=True)
class AuthFileSpec:
    """Where a kind's credential file lives on the host and in the container."""

    environment_variable: str
    default_relative_path: Path
    target_directory: str
    target_filename: str = "auth.json"


AUTH_FILE_DEFAULTS: dict[HarnessKind, AuthFileSpec] = {
    HarnessKind.MUSE: AuthFileSpec(
        "TTH_SANDBOX_MUSE_AUTH_FILE",
        Path(".config/muse/auth.json"),
        "/home/agent/.config/muse",
    ),
    HarnessKind.GROK: AuthFileSpec(
        "TTH_SANDBOX_GROK_AUTH_FILE",
        Path(".grok/auth.json"),
        "/home/agent/.grok",
    ),
    HarnessKind.CURSOR: AuthFileSpec(
        "TTH_SANDBOX_CURSOR_AUTH_FILE",
        Path(".config/cursor/auth.json"),
        "/home/agent/.config/cursor",
    ),
    HarnessKind.CODEX: AuthFileSpec(
        "TTH_SANDBOX_CODEX_AUTH_FILE",
        Path(".codex/auth.json"),
        "/home/agent/.codex",
    ),
    HarnessKind.CLAUDE: AuthFileSpec(
        "TTH_SANDBOX_CLAUDE_AUTH_FILE",
        Path(".claude/.credentials.json"),
        "/home/agent/.claude",
        target_filename=".credentials.json",
    ),
    HarnessKind.OPENCODE: AuthFileSpec(
        "TTH_SANDBOX_OPENCODE_AUTH_FILE",
        Path(".local/share/opencode/auth.json"),
        "/home/agent/.local/share/opencode",
    ),
    # ~/.prime/agent/auth.json holds third-party OAuth entries only; the
    # prime-inference credential (api_key + endpoints) lives in config.json.
    HarnessKind.PRIME_AGENT: AuthFileSpec(
        "TTH_SANDBOX_PRIME_AGENT_AUTH_FILE",
        Path(".prime/config.json"),
        "/home/agent/.prime",
        target_filename="config.json",
    ),
}


def auth_file_from_env(env: dict[str, str], spec: AuthFileSpec) -> str | None:
    """Resolve a kind's host auth file: explicit env var, else the $HOME default."""
    if spec.environment_variable in env:
        return env.get(spec.environment_variable) or None
    home = env.get("HOME")
    default = Path(home) / spec.default_relative_path if home else None
    return str(default) if default is not None and default.is_file() else None


def auth_files_from_env(env: dict[str, str]) -> dict[HarnessKind, str]:
    """Every kind whose host auth file resolves under ``env``."""
    auth_files: dict[HarnessKind, str] = {}
    for kind, spec in AUTH_FILE_DEFAULTS.items():
        auth_file = auth_file_from_env(env, spec)
        if auth_file is not None:
            auth_files[kind] = auth_file
    return auth_files


AuthSignature = tuple[str, int, int]


def auth_signature(auth_file: str | None) -> AuthSignature | None:
    """Cheap change detector: (path, mtime_ns, size); (-1, -1) when unreadable."""
    if auth_file is None:
        return None
    path = Path(auth_file)
    try:
        stat = path.stat()
    except OSError:
        return str(path), -1, -1
    return str(path), stat.st_mtime_ns, stat.st_size


def seed_auth_file(
    client: Any,
    mount_type: Any,
    *,
    kind: HarnessKind,
    auth_file: str,
    image: str,
    home_volume: str,
) -> None:
    """Copy the host auth file into the sandbox home volume via a one-shot container.

    The seeding container runs the split image with networking disabled and
    every capability dropped; the host file is bind-mounted read-only and
    copied to the kind's target directory with mode 0600.
    """
    source = Path(auth_file)
    if not source.is_file():
        raise DomainError(
            ErrorCode.SANDBOX_UNAVAILABLE,
            f"{kind.value.title()} auth file was not found",
            details={"kind": kind.value, "reason": "auth_file_missing", "path": str(source)},
        )
    spec = AUTH_FILE_DEFAULTS[kind]
    seed_path = f"/seed/{spec.target_filename}"
    client.containers.run(
        image,
        command=[
            "python",
            "-c",
            (
                "from pathlib import Path; import shutil; "
                f"target = Path({spec.target_directory!r}); "
                "target.mkdir(parents=True, exist_ok=True); "
                f"shutil.copy2({seed_path!r}, target / {spec.target_filename!r}); "
                f"(target / {spec.target_filename!r}).chmod(0o600)"
            ),
        ],
        mounts=[
            mount_type(
                target=seed_path,
                source=str(source),
                type="bind",
                read_only=True,
            ),
            mount_type(target="/home/agent", source=home_volume, type="volume"),
        ],
        network_disabled=True,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        remove=True,
    )
