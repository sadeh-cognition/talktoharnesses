"""Split image contract: the service runtime stays invisible to sandboxed agents."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from render_dockerfiles import SERVICE_VENV, SPLITS, render_dockerfile  # noqa: E402

_PER_KIND_MARKER = "# Per-kind from here on."


def _env_lines(dockerfile: str) -> list[str]:
    """Every ``ENV`` instruction with its continuation lines, joined."""
    lines: list[str] = []
    current: list[str] = []
    for raw in dockerfile.splitlines():
        if current:
            current.append(raw.strip())
            if not raw.rstrip().endswith("\\"):
                lines.append(" ".join(current))
                current = []
            continue
        if raw.startswith("ENV "):
            current = [raw.strip()]
            if not raw.rstrip().endswith("\\"):
                lines.append(raw.strip())
                current = []
    return lines


@pytest.mark.parametrize("kind", list(SPLITS))
def test_image_exports_no_uv_settings_and_keeps_the_venv_off_path(kind: str) -> None:
    dockerfile = render_dockerfile(SPLITS[kind])

    for env in _env_lines(dockerfile):
        assert "UV_" not in env, env
        assert SERVICE_VENV not in env, env
        assert "/opt/venv" not in env, env
    assert "UV_PROJECT_ENVIRONMENT=/opt/venv" not in dockerfile


@pytest.mark.parametrize("kind", list(SPLITS))
def test_service_runtime_is_root_owned_and_started_by_absolute_path(kind: str) -> None:
    dockerfile = render_dockerfile(SPLITS[kind])
    per_kind = dockerfile.split(_PER_KIND_MARKER, 1)[1]

    assert per_kind.count("USER root") == 1
    assert per_kind.rstrip().endswith("]")
    assert re.search(
        r'^CMD \["' + re.escape(SERVICE_VENV) + r'/bin/python", "-m", "uvicorn"', per_kind, re.M
    )
    assert "USER agent\n\nEXPOSE 8010" in per_kind
    assert per_kind.count(f"UV_PROJECT_ENVIRONMENT={SERVICE_VENV}") == 2
    # The build cache never lands under the service user's home (it is a volume).
    assert "/home/agent/.cache" not in per_kind
    assert "chown" not in per_kind


@pytest.mark.parametrize("kind", list(SPLITS))
def test_every_image_carries_node_corepack_and_uv_for_agents(kind: str) -> None:
    dockerfile = render_dockerfile(SPLITS[kind])

    assert "ARG NODE_MAJOR=22" in dockerfile
    assert "apt-get install -y --no-install-recommends nodejs" in dockerfile
    assert "corepack enable" in dockerfile
    assert "COPY --from=uv /uv /bin/uv" in dockerfile


def test_common_prefix_is_shared_between_kinds_with_the_same_installs() -> None:
    """BuildKit only reuses layers when the instructions above them are identical."""

    def prefix(kind: str) -> str:
        dockerfile = render_dockerfile(SPLITS[kind])
        head = dockerfile.split(_PER_KIND_MARKER, 1)[0]
        return head.replace(f"the {kind} harness", "the KIND harness").replace(
            f"bake {kind}", "bake KIND"
        )

    # codex and claude differ only in their package name, which is per-kind.
    assert prefix("codex") == prefix("claude")

    # The apt layer (everything before the harness installer) is shared by
    # every kind without extra apt packages.
    def apt_layer(kind: str) -> str:
        return prefix(kind).split("COPY --from=uv /uv /bin/uv", 1)[0]

    layers = {apt_layer(kind) for kind in SPLITS if not SPLITS[kind].extra_apt}
    assert len(layers) == 1


def test_rendered_dockerfiles_are_current() -> None:
    for kind, split in SPLITS.items():
        rendered = (ROOT / f"tth-{kind}" / "Dockerfile").read_text()
        assert rendered == render_dockerfile(split), f"tth-{kind}/Dockerfile is stale"
