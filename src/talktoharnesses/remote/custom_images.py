"""Derived sandbox images: a policy's Dockerfile text applied to a harness image.

A policy's ``image_dockerfile`` is built on the kind's base image (the one
``deploy/build-splits.sh`` produces) into ``tth-<kind>-custom:<hash>``, where
the hash covers the state root, the base image id and the text. Rebuilding a
base image changes the tag, so a derived image built on an older base is never
used: the next preparation builds a new one, and :func:`collect_garbage`
removes the old one once nothing uses it. Each state root builds and collects
only its own derived images.

The text is never built as written. :func:`render` writes the instructions
:func:`~tth_types.image_instructions.parse_image_instructions` accepted in a
canonical form, so BuildKit builds exactly what the policy validator read.

Everything here blocks; call it from a worker thread.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
from collections.abc import Generator, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError
from tth_types.image_instructions import ImageInstruction, parse_image_instructions

from talktoharnesses.remote import docker_ops
from talktoharnesses.remote.scope_layout import LOCKS_DIR, ScopeLayout

logger = logging.getLogger(__name__)

# Bump when rendering or verification changes, so images built under the old
# rules are rebuilt rather than reused.
CONTRACT_VERSION = 2
DERIVED_LABEL = "tth.derived"
# docker-bake.hcl sets it to base or gateway. Derived images would inherit
# "base" from their harness image, so the trailer sets "derived".
IMAGE_ROLE_LABEL = "tth.image"
# Which state root built a derived image; only that root collects it.
STATE_LABEL = "tth.state"
# A base image rebuilt during a derived build makes the build worthless; retry
# once on the new base before giving up.
_BASE_ATTEMPTS = 2
# Image settings the split service depends on. The policy's text cannot set
# most of them (the validator rejects those instructions); a build that
# changes any of them anyway is discarded.
_CONTRACT_CONFIG = (
    "User",
    "WorkingDir",
    "Entrypoint",
    "Cmd",
    "Healthcheck",
    "ExposedPorts",
    "Volumes",
    "StopSignal",
    "Shell",
    "OnBuild",
)
# What Docker uses when an image leaves the setting empty.
_CONFIG_DEFAULTS = {"User": "root", "WorkingDir": "/"}
# Restored after the policy's text, which may set them for its own steps.
_RESTORED_ENV = ("HOME", "DJANGO_SETTINGS_MODULE")
# Environment the split service or its Python runtime reads: a derived image
# must leave it as the harness image has it.
_SERVICE_ENV = frozenset({"HOME", "VIRTUAL_ENV"})
_SERVICE_ENV_PREFIXES = ("UV_", "PYTHON", "DJANGO_", "TALKTOHARNESSES_", "TTH_")
_DEFAULT_SHELL = ("/bin/sh", "-c")


def dockerfile_sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def state_id(state_root: Path) -> str:
    """Identifies a state root in derived image labels and tags."""
    return hashlib.sha256(str(state_root.resolve()).encode()).hexdigest()[:16]


def derived_tag(kind: HarnessKind, base_id: str, text_sha256: str, state: str) -> str:
    digest = hashlib.sha256(f"v{CONTRACT_VERSION}\n{state}\n{base_id}\n{text_sha256}".encode())
    return f"tth-{docker_ops.kind_slug(kind)}-custom:{digest.hexdigest()[:24]}"


def image_record(kind: HarnessKind, base_image: str, text: str) -> dict[str, object]:
    """What a scope writes to its ``image.json``: evidence that it wants the image."""
    return {
        "kind": kind.value,
        "base_image": base_image,
        "dockerfile_sha256": dockerfile_sha256(text),
        "contract": CONTRACT_VERSION,
    }


def render(
    base_tag: str,
    base: Mapping[str, Any],
    instructions: Iterable[ImageInstruction],
    labels: Mapping[str, str],
) -> str:
    """The derived Dockerfile: the policy's instructions between the base and a restoring trailer.

    Every instruction is one line, apart from COPY and ADD heredoc bodies, and
    RUN is a JSON exec form with ``<`` escaped, so BuildKit finds no options,
    continuations or heredocs the parser did not.
    """
    config = base["Config"]
    shell = tuple(config.get("Shell") or _DEFAULT_SHELL)
    env = docker_ops.image_environment(config)
    lines = [f"FROM {base_tag}", *(_render(instruction, shell) for instruction in instructions)]
    lines.append(f"WORKDIR {config.get('WorkingDir') or _CONFIG_DEFAULTS['WorkingDir']}")
    restored = {key: env[key] for key in _RESTORED_ENV if key in env}
    if restored:
        lines.append(
            "ENV " + " ".join(f"{key}={json.dumps(value)}" for key, value in restored.items())
        )
    lines.append(f"USER {config.get('User') or _CONFIG_DEFAULTS['User']}")
    lines.append("LABEL " + " ".join(f"{key}={json.dumps(value)}" for key, value in labels.items()))
    return "\n".join(lines) + "\n"


@contextlib.contextmanager
def image_lock(state_root: Path, tag: str, *, blocking: bool = True) -> Generator[bool]:
    """Hold the cross-process lock of one derived image; yields whether it was taken.

    Preparation holds it from looking the image up until its container exists,
    so garbage collection, which never blocks on it, cannot remove it meanwhile.
    """
    path = _lock_file(state_root, tag)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    while True:
        with path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            # Garbage collection deletes the file of a removed image while
            # holding its lock; a lock on a deleted file excludes no one.
            try:
                current = path.stat().st_ino == os.fstat(lock.fileno()).st_ino
            except FileNotFoundError:
                current = False
            if current:
                yield True
                return


@contextlib.contextmanager
def ensure_derived_image(
    client: Any,
    kind: HarnessKind,
    *,
    base_tag: str,
    dockerfile: str,
    state_root: Path,
    timeout: float,
) -> Generator[str]:
    """Yield the tag of ``dockerfile`` built on ``base_tag``, building it when missing.

    The image lock is held until the caller leaves the block. There is no
    fallback: an image that fails to build or to verify raises
    ``sandbox_unavailable`` with reason ``custom_image_build_failed``.
    """
    instructions = parse_image_instructions(dockerfile)
    text_sha256 = dockerfile_sha256(dockerfile)
    state = state_id(state_root)
    for _ in range(_BASE_ATTEMPTS):
        base = client.images.get(base_tag)
        tag = derived_tag(kind, base.id, text_sha256, state)
        labels = {
            IMAGE_ROLE_LABEL: "derived",
            DERIVED_LABEL: "1",
            STATE_LABEL: state,
            "tth.kind": kind.value,
            "tth.base-id": base.id,
            "tth.dockerfile-sha256": text_sha256,
            "tth.contract": str(CONTRACT_VERSION),
        }
        with image_lock(state_root, tag):
            if not _built(client, tag, labels):
                image = _build(
                    client,
                    kind,
                    tag=tag,
                    text=render(base_tag, base.attrs, instructions, labels),
                    state_root=state_root,
                    timeout=timeout,
                )
                # The build resolved FROM by tag. If the base was rebuilt
                # meanwhile, the image is not the one its tag and labels name.
                if client.images.get(base_tag).id != base.id:
                    logger.info("%s changed during the build of %s; building again", base_tag, tag)
                    _discard(client, image)
                    continue
                _verify(client, kind, tag=tag, image=image, base=base, labels=labels)
            yield tag
            return
    raise DomainError(
        ErrorCode.SANDBOX_UNAVAILABLE,
        f"{base_tag} changed during every build of its derived image",
        details={"kind": kind.value, "reason": "custom_image_build_failed"},
    )


def collect_garbage(
    client: Any, *, scopes: Iterable[ScopeLayout], state_root: Path, superseded: bool = True
) -> tuple[str, ...]:
    """Remove images nothing needs; returns their tags, or ids for untagged ones.

    A derived image of this state root stays while a container of any state
    uses it, while a scope under the state root still wants it on its current
    base image, or while a preparation holds its lock. Derived images of other
    state roots are never touched. With ``superseded``, untagged base and
    gateway images (the leftovers of rebuilding them) go too unless a
    container uses them.
    """
    used = {
        container.attrs.get("ImageID")
        for container in client.containers.list(all=True, sparse=True)
    }
    removed = _collect_derived(client, used, scopes=scopes, state_root=state_root)
    if superseded:
        removed += _collect_superseded(client, used)
    _sweep_locks(client, state_root)
    if removed:
        logger.info("removed %d unused sandbox images", len(removed))
    return tuple(removed)


def _collect_derived(
    client: Any, used: set[str | None], *, scopes: Iterable[ScopeLayout], state_root: Path
) -> list[str]:
    wanted = _wanted_tags(client, scopes=scopes, state_root=state_root)
    labels = [f"{DERIVED_LABEL}=1", f"{STATE_LABEL}={state_id(state_root)}"]
    removed: list[str] = []
    for image in client.images.list(filters={"label": labels}):
        if image.id in used or wanted.intersection(image.tags):
            continue
        if not image.tags:
            # Untagged, so no preparation can name it any more.
            if _remove(client, image.id, image.id):
                removed.append(image.id)
            continue
        tag = image.tags[0]
        with image_lock(state_root, tag, blocking=False) as locked:
            if locked and _remove(client, image.id, tag):
                _lock_file(state_root, tag).unlink(missing_ok=True)
                removed.append(tag)
    return removed


def _collect_superseded(client: Any, used: set[str | None]) -> list[str]:
    removed: list[str] = []
    for role in ("base", "gateway"):
        filters = {"label": f"{IMAGE_ROLE_LABEL}={role}", "dangling": True}
        for image in client.images.list(filters=filters):
            if image.id not in used and _remove(client, image.id, image.id):
                removed.append(image.id)
    return removed


def _sweep_locks(client: Any, state_root: Path) -> None:
    """Delete the lock files of derived images that no longer exist."""
    from docker.errors import ImageNotFound

    locks = state_root / LOCKS_DIR / "images"
    if not locks.is_dir():
        return
    for path in locks.glob("*.lock"):
        repository, _, digest = path.stem.rpartition("_")
        if not repository:
            continue
        tag = f"{repository}:{digest}"
        with image_lock(state_root, tag, blocking=False) as locked:
            if not locked:
                continue
            try:
                client.images.get(tag)
            except ImageNotFound:
                path.unlink(missing_ok=True)


def _remove(client: Any, image_id: str, name: str) -> bool:
    from docker.errors import APIError, NotFound

    try:
        client.images.remove(image_id, force=False)
    except NotFound:
        return False
    except APIError as exc:
        # Typically a container created since the survey.
        logger.info("kept sandbox image %s: %s", name, exc)
        return False
    return True


def _wanted_tags(client: Any, *, scopes: Iterable[ScopeLayout], state_root: Path) -> set[str]:
    """The derived tags scopes would use on their base images as they are today."""
    from docker.errors import ImageNotFound

    state = state_id(state_root)
    base_ids: dict[str, str | None] = {}
    wanted: set[str] = set()
    for layout in scopes:
        try:
            record = json.loads(layout.image_file.read_text())
            kind = HarnessKind(record["kind"])
            base_image = str(record["base_image"])
            text_sha256 = str(record["dockerfile_sha256"])
            contract = record["contract"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if contract != CONTRACT_VERSION:
            continue
        if base_image not in base_ids:
            try:
                base_ids[base_image] = client.images.get(base_image).id
            except ImageNotFound:
                base_ids[base_image] = None
        if (base_id := base_ids[base_image]) is not None:
            wanted.add(derived_tag(kind, base_id, text_sha256, state))
    return wanted


def _lock_file(state_root: Path, tag: str) -> Path:
    return state_root / LOCKS_DIR / "images" / (tag.replace(":", "_") + ".lock")


def _render(instruction: ImageInstruction, shell: Sequence[str]) -> str:
    keyword = instruction.keyword
    if keyword == "RUN":
        argv = instruction.exec_form or (*shell, _script(instruction))
        return f"RUN {_json(argv)}"
    if keyword in {"COPY", "ADD"}:
        head = " ".join((keyword, *instruction.options))
        if instruction.heredocs:
            # The parser allowed only plain words and heredoc words here, which
            # BuildKit splits and recognizes exactly as the parser did.
            bodies = [
                line for heredoc in instruction.heredocs for line in (*heredoc.lines, heredoc.end)
            ]
            return "\n".join([f"{head} {' '.join(instruction.words)}", *bodies])
        return f"{head} {_json(instruction.exec_form or instruction.words)}"
    return f"{keyword} {instruction.arguments}"


def _script(instruction: ImageInstruction) -> str:
    """The shell script a shell-form RUN stands for, heredoc bodies included."""
    if not instruction.heredocs:
        return instruction.arguments
    first, *more = instruction.heredocs
    if not more and instruction.arguments == first.word:
        # RUN <<EOF runs the body itself, as its own executable after a #! line.
        return _executable(first.content) if first.content.startswith("#!") else first.content
    bodies = [line for heredoc in instruction.heredocs for line in (*heredoc.lines, heredoc.end)]
    return "\n".join([instruction.arguments, *bodies]) + "\n"


def _executable(content: str) -> str:
    end = "TTH_SCRIPT"
    while end in content.split("\n"):
        end += "_"
    return (
        'tth_script="$(mktemp)" || exit 1\n'
        f"cat > \"$tth_script\" <<'{end}'\n{content}{end}\n"
        'chmod +x "$tth_script" && "$tth_script"\n'
        'tth_status=$?\nrm -f "$tth_script"\nexit "$tth_status"\n'
    )


def _json(argv: Sequence[str]) -> str:
    # "<" never occurs in JSON outside strings, so escaping it leaves no "<<"
    # for BuildKit's heredoc detection.
    return json.dumps(list(argv)).replace("<", "\\u003c")


def _built(client: Any, tag: str, labels: Mapping[str, str]) -> bool:
    from docker.errors import ImageNotFound

    try:
        image = client.images.get(tag)
    except ImageNotFound:
        return False
    return _labelled(image.attrs, labels)


def _build(
    client: Any, kind: HarnessKind, *, tag: str, text: str, state_root: Path, timeout: float
) -> Any:
    docker_bin = docker_ops.ensure_docker_cli_available(kind)
    # The daemon's own builder: it resolves FROM against the local harness
    # image and leaves the result in the local image store.
    builder = docker_ops.docker_driver_builder(
        docker_bin, kind=kind, reason="custom_image_build_failed"
    )
    # No build context: the text can only fetch over the network or copy from
    # other images, never read files from this host.
    docker_ops.run_image_build(
        [docker_bin, "buildx", "build", "--builder", builder, "--load"]
        + ["--progress=plain", "-t", tag, "-"],
        kind=kind,
        image=tag,
        cwd=state_root,
        timeout=timeout,
        stdin=text,
        reason="custom_image_build_failed",
    )
    return client.images.get(tag)


def _verify(
    client: Any, kind: HarnessKind, *, tag: str, image: Any, base: Any, labels: Mapping[str, str]
) -> None:
    problem = _contract_problem(image.attrs, base.attrs, labels)
    if problem is None:
        return
    logger.error("discarding sandbox image %s: %s", tag, problem)
    _discard(client, image)
    raise DomainError(
        ErrorCode.SANDBOX_UNAVAILABLE,
        f"sandbox image {tag} {problem}",
        details={"kind": kind.value, "reason": "custom_image_build_failed"},
    )


def _discard(client: Any, image: Any) -> None:
    with contextlib.suppress(Exception):
        client.images.remove(image.id, force=True)


def _contract_problem(
    derived: Mapping[str, Any], base: Mapping[str, Any], labels: Mapping[str, str]
) -> str | None:
    """Why ``derived`` cannot run the split service, or None when it can."""
    derived_config, base_config = derived["Config"], base["Config"]
    for key in _CONTRACT_CONFIG:
        default = _CONFIG_DEFAULTS.get(key)
        if (derived_config.get(key) or default) != (base_config.get(key) or default):
            return f"changes {key} of the harness image"
    derived_env = docker_ops.image_environment(derived_config)
    base_env = docker_ops.image_environment(base_config)
    for key in sorted(derived_env.keys() | base_env.keys()):
        if key not in _SERVICE_ENV and not key.startswith(_SERVICE_ENV_PREFIXES):
            continue
        if derived_env.get(key) != base_env.get(key):
            change = "changes" if key in base_env else "sets"
            return f"{change} {key}, which the split service reads"
    if not _keeps_entries(base_env.get("PATH"), derived_env.get("PATH")):
        return "removes directories from PATH"
    base_layers = base["RootFS"]["Layers"]
    if derived["RootFS"]["Layers"][: len(base_layers)] != base_layers:
        return "is not built on the harness image"
    if not _labelled(derived, labels):
        return "is missing its labels"
    return None


def _keeps_entries(base_path: str | None, derived_path: str | None) -> bool:
    """Whether ``derived_path`` still lists every directory of ``base_path``, in order."""
    if base_path is None:
        return True
    remaining = iter((derived_path or "").split(":"))
    return all(entry in remaining for entry in base_path.split(":"))


def _labelled(attrs: Mapping[str, Any], labels: Mapping[str, str]) -> bool:
    actual: dict[str, str] = attrs["Config"].get("Labels") or {}
    return all(actual.get(key) == value for key, value in labels.items())
