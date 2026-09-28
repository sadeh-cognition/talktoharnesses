"""Policy image instructions: canonical rendering, derived builds, and image cleanup."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from docker.errors import APIError, ImageNotFound
from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError, public_message
from tth_types.image_instructions import parse_image_instructions

from talktoharnesses.remote import docker_ops
from talktoharnesses.remote.custom_images import (
    CONTRACT_VERSION,
    collect_garbage,
    derived_tag,
    dockerfile_sha256,
    ensure_derived_image,
    image_lock,
    image_record,
    render,
    state_id,
)
from talktoharnesses.remote.scope_layout import ScopeLayout

BASE_TAG = "tth-codex:latest"
TEXT = "USER root\nRUN apt-get update && apt-get install -y g++"
BASE_ATTRS: dict[str, Any] = {
    "Config": {
        "User": "agent",
        "WorkingDir": "/app/service",
        "Env": [
            "PATH=/home/agent/.local/bin:/usr/bin",
            "HOME=/home/agent",
            "DJANGO_SETTINGS_MODULE=tth_codex.settings",
            "PYTHONUNBUFFERED=1",
        ],
        "Cmd": ["/opt/tth/venv/bin/python", "-m", "uvicorn"],
        "Healthcheck": {"Test": ["CMD-SHELL", "curl -fsS http://127.0.0.1:8010/v1/health"]},
        "ExposedPorts": {"8010/tcp": {}},
        "Labels": {"tth.image": "base"},
    },
    "RootFS": {"Layers": ["sha256:l1", "sha256:l2"]},
}


class FakeImage:
    def __init__(
        self, image_id: str, tags: Sequence[str] = (), attrs: dict[str, Any] | None = None
    ):
        self.id = image_id
        self.tags = list(tags)
        self.attrs = attrs or {"Config": {"Labels": {}}, "RootFS": {"Layers": []}}


class FakeImages:
    def __init__(self) -> None:
        self.items: list[FakeImage] = []
        self.remove_errors: dict[str, Exception] = {}
        self.removed: list[tuple[str, bool]] = []

    def add(self, image: FakeImage) -> FakeImage:
        # Like Docker, a new image takes its tags from the images that had them.
        for other in self.items:
            other.tags = [tag for tag in other.tags if tag not in image.tags]
        self.items.append(image)
        return image

    def get(self, name: str) -> FakeImage:
        for image in self.items:
            if name == image.id or name in image.tags:
                return image
        raise ImageNotFound(name)

    def list(self, filters: dict[str, Any]) -> list[FakeImage]:
        wanted = filters["label"]
        found: list[FakeImage] = []
        for image in self.items:
            labels: dict[str, str] = image.attrs["Config"].get("Labels") or {}
            if not all(
                key in labels and (not value or labels[key] == value)
                for key, _, value in (
                    label.partition("=")
                    for label in ([wanted] if isinstance(wanted, str) else wanted)
                )
            ):
                continue
            if filters.get("dangling") and image.tags:
                continue
            found.append(image)
        return found

    def remove(self, image: str, force: bool = False) -> None:
        self.removed.append((image, force))
        if image in self.remove_errors:
            raise self.remove_errors[image]
        self.items.remove(self.get(image))


class FakeContainer:
    def __init__(self, image_id: str) -> None:
        self.attrs = {"ImageID": image_id}


class FakeContainers:
    def __init__(self) -> None:
        self.items: list[FakeContainer] = []

    def list(self, *, all: bool, sparse: bool) -> list[FakeContainer]:
        assert all and sparse
        return self.items


class FakeDocker:
    def __init__(self) -> None:
        self.images = FakeImages()
        self.containers = FakeContainers()
        self.base = self.images.add(FakeImage("sha256:base", [BASE_TAG], BASE_ATTRS))


def _labels(dockerfile: str) -> dict[str, str]:
    line = [line for line in dockerfile.splitlines() if line.startswith("LABEL ")][-1]
    return dict(re.findall(r'(\S+)="([^"]*)"', line))


def _derived_attrs(dockerfile: str, base: dict[str, Any] = BASE_ATTRS) -> dict[str, Any]:
    """What Docker makes of a rendered Dockerfile that keeps the contract."""
    config: dict[str, Any] = {
        **base["Config"],
        # Labels are inherited from the base image and overridden by the trailer.
        "Labels": {**(base["Config"].get("Labels") or {}), **_labels(dockerfile)},
        "User": base["Config"].get("User") or "root",
        "WorkingDir": base["Config"].get("WorkingDir") or "/",
    }
    return {"Config": config, "RootFS": {"Layers": [*base["RootFS"]["Layers"], "sha256:custom"]}}


@pytest.fixture
def docker() -> FakeDocker:
    return FakeDocker()


@pytest.fixture
def builds(monkeypatch: pytest.MonkeyPatch, docker: FakeDocker) -> list[dict[str, Any]]:
    """Record builds; each one adds an image that honors the base contract."""
    recorded: list[dict[str, Any]] = []

    def build(command: Sequence[str], **options: Any) -> None:
        recorded.append({"command": list(command), **options})
        base = docker.images.get(BASE_TAG).attrs
        docker.images.add(
            FakeImage(
                f"sha256:derived{len(recorded)}",
                [options["image"]],
                _derived_attrs(options["stdin"], base),
            )
        )

    def docker_cli(kind: HarnessKind | None = None) -> str:
        del kind
        return "docker"

    def builder(docker_bin: str, **options: Any) -> str:
        del docker_bin, options
        return "desktop-linux"

    monkeypatch.setattr(docker_ops, "run_image_build", build)
    monkeypatch.setattr(docker_ops, "ensure_docker_cli_available", docker_cli)
    monkeypatch.setattr(docker_ops, "docker_driver_builder", builder)
    return recorded


def _ensure(docker: FakeDocker, tmp_path: Path, text: str = TEXT, base_tag: str = BASE_TAG) -> Any:
    return ensure_derived_image(
        docker,
        HarnessKind.CODEX,
        base_tag=base_tag,
        dockerfile=text,
        state_root=tmp_path,
        timeout=60,
    )


def _tag(tmp_path: Path, base_id: str = "sha256:base", text: str = TEXT) -> str:
    return derived_tag(HarnessKind.CODEX, base_id, dockerfile_sha256(text), state_id(tmp_path))


# --- naming and rendering ----------------------------------------------------


def test_tag_follows_the_state_root_the_base_image_and_the_text(tmp_path: Path) -> None:
    sha = dockerfile_sha256(TEXT)
    state = state_id(tmp_path)
    tag = derived_tag(HarnessKind.CODEX, "sha256:base", sha, state)
    assert re.fullmatch(r"tth-codex-custom:[0-9a-f]{24}", tag)
    assert tag == derived_tag(HarnessKind.CODEX, "sha256:base", sha, state_id(tmp_path / "."))
    assert tag != derived_tag(HarnessKind.CODEX, "sha256:rebuilt", sha, state)
    assert tag != derived_tag(HarnessKind.CODEX, "sha256:base", dockerfile_sha256("RUN x"), state)
    assert tag != derived_tag(HarnessKind.CLAUDE, "sha256:base", sha, state)
    assert tag != derived_tag(HarnessKind.CODEX, "sha256:base", sha, state_id(tmp_path / "other"))
    assert image_record(HarnessKind.CODEX, BASE_TAG, TEXT) == {
        "kind": "codex",
        "base_image": BASE_TAG,
        "dockerfile_sha256": sha,
        "contract": CONTRACT_VERSION,
    }


def test_rendered_dockerfile_is_canonical_and_restores_the_split_contract() -> None:
    text = (
        "USER root\n"
        "RUN apt-get update \\\n"
        "    # a comment\n"
        "    && apt-get install -y g++\n"
        "RUN echo $((1<<3)) > /n\n"
        'RUN ["echo", "<<EOF"]\n'
        "RUN cat <<EOF > /etc/motd\n"
        "hello\n"
        "EOF\n"
        "RUN <<EOF\n"
        "echo script\n"
        "EOF\n"
        "COPY --chmod=644 <<'EOT' /etc/\n"
        "$HOME stays\n"
        "EOT\n"
        "COPY --from=rust:1 /usr/local/cargo /opt/cargo\n"
        'ENV A=1 B="two words"'
    )
    labels = {"tth.image": "derived", "tth.kind": "codex"}

    dockerfile = render(BASE_TAG, BASE_ATTRS, parse_image_instructions(text), labels)

    assert dockerfile.splitlines() == [
        "FROM tth-codex:latest",
        "USER root",
        'RUN ["/bin/sh", "-c", "apt-get update     && apt-get install -y g++"]',
        r'RUN ["/bin/sh", "-c", "echo $((1\u003c\u003c3)) > /n"]',
        r'RUN ["echo", "\u003c\u003cEOF"]',
        r'RUN ["/bin/sh", "-c", "cat \u003c\u003cEOF > /etc/motd\nhello\nEOF\n"]',
        r'RUN ["/bin/sh", "-c", "echo script\n"]',
        "COPY --chmod=644 <<'EOT' /etc/",
        "$HOME stays",
        "EOT",
        'COPY --from=rust:1 ["/usr/local/cargo", "/opt/cargo"]',
        'ENV A=1 B="two words"',
        "WORKDIR /app/service",
        'ENV HOME="/home/agent" DJANGO_SETTINGS_MODULE="tth_codex.settings"',
        "USER agent",
        'LABEL tth.image="derived" tth.kind="codex"',
    ]
    # BuildKit sees no heredoc outside the COPY that has one.
    assert dockerfile.count("<<") == 1


@pytest.mark.parametrize(
    ("text", "output"),
    [
        ("RUN cat <<EOF && cat <<-'END'\none $X\nEOF\n\ttwo $X\n\tEND", "one x\ntwo $X\n"),
        ("RUN <<EOF\necho a\necho b\nEOF", "a\nb\n"),
        # The body holds the name the wrapper would use to end it.
        ("RUN <<'EOF'\n#!/bin/sh\necho executable \"$0\"\nexit 0\nTTH_SCRIPT\nEOF", "executable"),
    ],
)
def test_rendered_run_scripts_behave_like_docker_heredocs(text: str, output: str) -> None:
    [line] = [
        line
        for line in render(BASE_TAG, BASE_ATTRS, parse_image_instructions(text), {}).splitlines()
        if line.startswith("RUN ")
    ]
    argv = json.loads(line.removeprefix("RUN "))
    result = subprocess.run(
        argv, capture_output=True, text=True, check=True, env={"X": "x", "PATH": "/usr/bin:/bin"}
    )
    assert result.stdout.startswith(output)


def test_base_without_user_or_workdir_gets_docker_defaults(
    docker: FakeDocker, builds: list[dict[str, Any]], tmp_path: Path
) -> None:
    config = {**BASE_ATTRS["Config"], "User": "", "WorkingDir": ""}
    docker.base.attrs = {**BASE_ATTRS, "Config": config}

    with _ensure(docker, tmp_path) as tag:
        pass

    [build] = builds
    assert "\nWORKDIR /\n" in build["stdin"] and "\nUSER root\n" in build["stdin"]
    docker.images.get(tag)


# --- building ----------------------------------------------------------------


def test_missing_image_is_built_by_the_daemon_builder_and_locked_while_in_use(
    docker: FakeDocker, builds: list[dict[str, Any]], tmp_path: Path
) -> None:
    with _ensure(docker, tmp_path) as tag:
        assert tag == _tag(tmp_path)
        with image_lock(tmp_path, tag, blocking=False) as locked:
            assert not locked
    with image_lock(tmp_path, tag, blocking=False) as locked:
        assert locked
    [build] = builds
    assert build["command"] == [
        "docker",
        "buildx",
        "build",
        "--builder",
        "desktop-linux",
        "--load",
        "--progress=plain",
        "-t",
        tag,
        "-",
    ]
    assert build["reason"] == "custom_image_build_failed"
    assert build["cwd"] == tmp_path and build["timeout"] == 60
    assert build["stdin"].startswith('FROM tth-codex:latest\nUSER root\nRUN ["/bin/sh", "-c", ')
    labels = docker.images.get(tag).attrs["Config"]["Labels"]
    assert labels["tth.base-id"] == "sha256:base" and labels["tth.contract"] == "2"
    assert labels["tth.state"] == state_id(tmp_path) and labels["tth.image"] == "derived"


def test_existing_image_is_reused_and_a_mislabelled_one_rebuilt(
    docker: FakeDocker, builds: list[dict[str, Any]], tmp_path: Path
) -> None:
    with _ensure(docker, tmp_path) as tag:
        pass
    with _ensure(docker, tmp_path) as again:
        assert again == tag
    assert len(builds) == 1
    docker.images.get(tag).attrs["Config"]["Labels"]["tth.dockerfile-sha256"] = "other"
    with _ensure(docker, tmp_path):
        pass
    assert len(builds) == 2


def test_base_tags_of_one_image_share_its_derived_image(
    docker: FakeDocker, builds: list[dict[str, Any]], tmp_path: Path
) -> None:
    docker.base.tags.append("tth-codex:v1")

    with _ensure(docker, tmp_path) as first:
        pass
    with _ensure(docker, tmp_path, base_tag="tth-codex:v1") as second:
        pass
    with _ensure(docker, tmp_path) as third:
        pass

    assert first == second == third and len(builds) == 1


def test_base_rebuilt_during_the_build_is_built_on_again(
    docker: FakeDocker,
    builds: list[dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = docker_ops.run_image_build
    rebuilds = iter([1])

    def racing_build(command: Sequence[str], **options: Any) -> None:
        build(command, **options)
        # deploy/build-splits.sh retags the base while the first build runs.
        for generation in rebuilds:
            attrs = {**BASE_ATTRS, "RootFS": {"Layers": ["sha256:l1", f"sha256:new{generation}"]}}
            docker.images.add(FakeImage("sha256:rebuilt", [BASE_TAG], attrs))

    monkeypatch.setattr(docker_ops, "run_image_build", racing_build)

    with _ensure(docker, tmp_path) as tag:
        assert tag == _tag(tmp_path, "sha256:rebuilt")

    assert len(builds) == 2
    assert docker.images.removed == [("sha256:derived1", True)]
    assert docker.images.get(tag).attrs["Config"]["Labels"]["tth.base-id"] == "sha256:rebuilt"


def test_base_rebuilt_during_every_build_fails_preparation(
    docker: FakeDocker,
    builds: list[dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = docker_ops.run_image_build

    def racing_build(command: Sequence[str], **options: Any) -> None:
        build(command, **options)
        docker.images.add(FakeImage(f"sha256:rebuilt{len(builds)}", [BASE_TAG], BASE_ATTRS))

    monkeypatch.setattr(docker_ops, "run_image_build", racing_build)

    with pytest.raises(DomainError) as raised, _ensure(docker, tmp_path):
        pass

    assert raised.value.details["reason"] == "custom_image_build_failed"
    assert "changed during every build" in raised.value.message
    assert len(builds) == 2


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"User": "root"}, "changes User"),
        ({"Cmd": ["sleep", "infinity"]}, "changes Cmd"),
        ({"Env": ["HOME=/root", "DJANGO_SETTINGS_MODULE=tth_codex.settings"]}, "changes HOME"),
        ({"Env": [*BASE_ATTRS["Config"]["Env"], "UV_PROJECT_ENVIRONMENT=/x"]}, "sets UV_PROJECT"),
        ({"Env": [*BASE_ATTRS["Config"]["Env"], "PYTHONPATH=/opt/libs"]}, "sets PYTHONPATH"),
        ({"Env": [*BASE_ATTRS["Config"]["Env"], "VIRTUAL_ENV=/opt/tth/venv"]}, "sets VIRTUAL_ENV"),
        (
            {"Env": [*BASE_ATTRS["Config"]["Env"], "TALKTOHARNESSES_CODEX_EXECUTABLE=/x"]},
            "sets TALKTOHARNESSES_CODEX_EXECUTABLE, which the split service reads",
        ),
        (
            {"Env": [*BASE_ATTRS["Config"]["Env"][1:], "PATH=/usr/bin", "PYTHONUNBUFFERED=1"]},
            "removes directories from PATH",
        ),
        ({"Labels": {}}, "missing its labels"),
        ({}, "is not built on the harness image"),
    ],
)
def test_image_that_breaks_the_split_contract_is_discarded(
    docker: FakeDocker,
    builds: list[dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    change: dict[str, Any],
    problem: str,
) -> None:
    def sabotage(command: Sequence[str], **options: Any) -> None:
        del command
        attrs = _derived_attrs(options["stdin"])
        attrs["Config"].update(change)
        if not change:
            # An empty change stands for a build on some other base image.
            attrs["RootFS"]["Layers"] = ["sha256:other"]
        docker.images.add(FakeImage("sha256:bad", [options["image"]], attrs))

    monkeypatch.setattr(docker_ops, "run_image_build", sabotage)
    with pytest.raises(DomainError) as raised, _ensure(docker, tmp_path):
        pass
    assert raised.value.code is ErrorCode.SANDBOX_UNAVAILABLE
    assert raised.value.details["reason"] == "custom_image_build_failed"
    assert problem in raised.value.message and problem in caplog.text
    assert docker.images.removed == [("sha256:bad", True)]
    assert builds == []
    assert "check TalkToHarnesses server logs" in public_message(
        raised.value.code, details=raised.value.details
    )


def test_image_may_extend_path_and_set_its_own_variables(
    docker: FakeDocker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    builds: list[dict[str, Any]],
) -> None:
    def extend(command: Sequence[str], **options: Any) -> None:
        del command
        attrs = _derived_attrs(options["stdin"])
        attrs["Config"]["Env"] = [
            "PATH=/opt/cargo/bin:/home/agent/.local/bin:/usr/local/bin:/usr/bin",
            *BASE_ATTRS["Config"]["Env"][1:],
            "CARGO_HOME=/opt/cargo",
            "LANG=C.UTF-8",
        ]
        docker.images.add(FakeImage("sha256:rust", [options["image"]], attrs))

    monkeypatch.setattr(docker_ops, "run_image_build", extend)

    with _ensure(docker, tmp_path) as tag:
        assert docker.images.get(tag).id == "sha256:rust"


# --- cleanup -----------------------------------------------------------------


def _scope(tmp_path: Path, name: str, record: dict[str, Any] | None) -> ScopeLayout:
    layout = ScopeLayout(name, tmp_path)
    layout.state.mkdir(parents=True)
    if record is not None:
        layout.image_file.write_text(json.dumps(record))
    return layout


def _derived(
    docker: FakeDocker, image_id: str, tag: str | None, state_root: Path, **labels: str
) -> FakeImage:
    labels = {
        "tth.image": "derived",
        "tth.derived": "1",
        "tth.state": state_id(state_root),
        **labels,
    }
    return docker.images.add(
        FakeImage(image_id, [tag] if tag else [], {"Config": {"Labels": labels}})
    )


def test_cleanup_keeps_used_and_wanted_images_and_removes_the_rest(
    docker: FakeDocker, tmp_path: Path
) -> None:
    sha = dockerfile_sha256(TEXT)
    wanted = _tag(tmp_path)
    superseded = _tag(tmp_path, "sha256:old-base")
    _derived(docker, "sha256:wanted", wanted, tmp_path)
    _derived(docker, "sha256:superseded", superseded, tmp_path)
    _derived(docker, "sha256:running", "tth-codex-custom:" + "c" * 24, tmp_path)
    _derived(docker, "sha256:dangling", None, tmp_path)
    docker.containers.items.append(FakeContainer("sha256:running"))
    scopes = [
        _scope(tmp_path, "tth-scope-" + "a" * 24, image_record(HarnessKind.CODEX, BASE_TAG, TEXT)),
        _scope(tmp_path, "tth-scope-" + "b" * 24, None),
        _scope(tmp_path, "tth-scope-" + "c" * 24, {"kind": "codex"}),
        # Written under an older contract: its images are rebuilt, not kept.
        _scope(
            tmp_path,
            "tth-scope-" + "d" * 24,
            {
                "kind": "codex",
                "base_image": BASE_TAG,
                "dockerfile_sha256": sha,
                "contract": CONTRACT_VERSION - 1,
            },
        ),
        # A base image that does not exist wants nothing.
        _scope(
            tmp_path,
            "tth-scope-" + "e" * 24,
            image_record(HarnessKind.CLAUDE, "tth-claude:latest", TEXT),
        ),
    ]

    removed = collect_garbage(docker, scopes=scopes, state_root=tmp_path)

    # The dangling derived image carries the role "derived", so it goes once.
    assert sorted(removed) == sorted([superseded, "sha256:dangling"])
    assert {image.id for image in docker.images.items} == {
        "sha256:base",
        "sha256:wanted",
        "sha256:running",
    }
    assert all(not force for _, force in docker.images.removed)


def test_cleanup_wants_the_base_image_each_scope_recorded(
    docker: FakeDocker, tmp_path: Path
) -> None:
    docker.images.add(FakeImage("sha256:blue", ["tth-codex:blue"], BASE_ATTRS))
    blue = _tag(tmp_path, "sha256:blue")
    _derived(docker, "sha256:derived-blue", blue, tmp_path)
    scope = _scope(
        tmp_path, "tth-scope-" + "a" * 24, image_record(HarnessKind.CODEX, "tth-codex:blue", TEXT)
    )

    assert collect_garbage(docker, scopes=[scope], state_root=tmp_path) == ()
    docker.images.get(blue)


def test_cleanup_leaves_other_state_roots_images_alone(docker: FakeDocker, tmp_path: Path) -> None:
    other_root = tmp_path / "other"
    _derived(docker, "sha256:theirs", "tth-codex-custom:" + "1" * 24, other_root)
    _derived(docker, "sha256:theirs-untagged", None, other_root)
    _derived(docker, "sha256:mine", "tth-codex-custom:" + "2" * 24, tmp_path)

    removed = collect_garbage(docker, scopes=[], state_root=tmp_path)

    assert removed == ("tth-codex-custom:" + "2" * 24,)
    assert {"sha256:theirs", "sha256:theirs-untagged"} <= {i.id for i in docker.images.items}


def test_cleanup_removes_untagged_base_and_gateway_images_nothing_runs(
    docker: FakeDocker, tmp_path: Path
) -> None:
    for image_id, role in (
        ("sha256:old-base", "base"),
        ("sha256:old-gateway", "gateway"),
        ("sha256:kept", "base"),
    ):
        docker.images.add(FakeImage(image_id, [], {"Config": {"Labels": {"tth.image": role}}}))
    docker.containers.items.append(FakeContainer("sha256:kept"))

    assert collect_garbage(docker, scopes=[], state_root=tmp_path, superseded=False) == ()
    removed = collect_garbage(docker, scopes=[], state_root=tmp_path)

    # The tagged base image is never a leftover.
    assert set(removed) == {"sha256:old-base", "sha256:old-gateway"}
    assert {image.id for image in docker.images.items} == {"sha256:base", "sha256:kept"}


def test_cleanup_skips_locked_in_use_and_vanished_images(
    docker: FakeDocker, tmp_path: Path
) -> None:
    locked = _derived(docker, "sha256:locked", "tth-codex-custom:" + "1" * 24, tmp_path)
    _derived(docker, "sha256:conflict", "tth-codex-custom:" + "2" * 24, tmp_path)
    _derived(docker, "sha256:gone", "tth-codex-custom:" + "3" * 24, tmp_path)
    docker.images.remove_errors["sha256:conflict"] = APIError("image is being used")
    docker.images.remove_errors["sha256:gone"] = ImageNotFound("gone")

    with image_lock(tmp_path, locked.tags[0]):
        removed = collect_garbage(docker, scopes=[], state_root=tmp_path)

    assert removed == ()
    assert len(docker.images.items) == 4


def test_cleanup_deletes_the_lock_files_of_images_that_are_gone(
    docker: FakeDocker, tmp_path: Path
) -> None:
    removed_tag = "tth-codex-custom:" + "1" * 24
    kept_tag = "tth-codex-custom:" + "2" * 24
    _derived(docker, "sha256:removed", removed_tag, tmp_path)
    _derived(docker, "sha256:kept", kept_tag, tmp_path)
    docker.containers.items.append(FakeContainer("sha256:kept"))
    # Locks of an image removed some other way, and of a build that failed.
    for tag in (removed_tag, kept_tag, "tth-codex-custom:" + "3" * 24):
        with image_lock(tmp_path, tag):
            pass
    locks = tmp_path / ".locks" / "images"
    assert len(list(locks.iterdir())) == 3

    assert collect_garbage(docker, scopes=[], state_root=tmp_path) == (removed_tag,)

    assert [path.name for path in locks.iterdir()] == ["tth-codex-custom_" + "2" * 24 + ".lock"]


def test_waiter_on_a_deleted_lock_file_takes_the_new_one(tmp_path: Path) -> None:
    tag = "tth-codex-custom:" + "1" * 24
    path = tmp_path / ".locks" / "images" / ("tth-codex-custom_" + "1" * 24 + ".lock")
    taken = threading.Event()
    held: list[bool] = []

    def wait() -> None:
        with image_lock(tmp_path, tag) as locked:
            held.append(locked and path.exists())
            taken.set()

    with image_lock(tmp_path, tag):
        waiter = threading.Thread(target=wait)
        waiter.start()
        assert not taken.wait(0.2)
        # What garbage collection does after removing the image.
        path.unlink()
    waiter.join(5)
    assert held == [True]


# --- docker CLI ----------------------------------------------------------------


def test_build_output_that_is_not_utf8_still_fails_cleanly(tmp_path: Path) -> None:
    script = "import sys; sys.stdout.buffer.write(b'caf\\xe9 \\xff\\n'); sys.exit(1)"
    with pytest.raises(DomainError) as raised:
        docker_ops.run_image_build(
            [sys.executable, "-c", script],
            kind=HarnessKind.CODEX,
            image="tth-codex-custom:x",
            cwd=tmp_path,
            timeout=30,
            stdin="FROM x\n",
            reason="custom_image_build_failed",
        )
    assert raised.value.details["reason"] == "custom_image_build_failed"
    assert "caf\ufffd \ufffd" in raised.value.details["build_tail"]


def test_daemon_builder_is_named_after_the_docker_context(tmp_path: Path) -> None:
    docker = tmp_path / "docker"
    docker.write_text('#!/bin/sh\n[ "$1 $2" = "context show" ] && echo desktop-linux\n')
    docker.chmod(0o755)
    broken = tmp_path / "broken"
    broken.write_text("#!/bin/sh\necho 'no context' >&2\nexit 1\n")
    broken.chmod(0o755)

    assert (
        docker_ops.docker_driver_builder(str(docker), kind=HarnessKind.CODEX, reason="r")
        == "desktop-linux"
    )
    with pytest.raises(DomainError) as raised:
        docker_ops.docker_driver_builder(str(broken), kind=HarnessKind.CODEX, reason="r")
    assert raised.value.details == {"kind": "codex", "reason": "r"}
