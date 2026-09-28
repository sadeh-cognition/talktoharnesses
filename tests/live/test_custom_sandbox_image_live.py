"""Opt-in derived image gate using real Docker and the local tth-codex image.

TALKTOHARNESSES_SANDBOX_DOCKER=1 enables the test. It needs ``tth-codex:<tag>``
(TTH_SANDBOX_IMAGE_TAG, default ``latest``) and builds only small images on
top of it; everything it creates is removed afterwards. Its image cleanup runs
under a temporary state root, so it never touches another state root's images
or the untagged harness images a real proxy would remove.
"""

from __future__ import annotations

import json
import os
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from tth_types.enums import HarnessKind

from talktoharnesses.remote import custom_images
from talktoharnesses.remote.scope_layout import ScopeLayout

pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_SANDBOX_DOCKER") != "1",
    reason="set TALKTOHARNESSES_SANDBOX_DOCKER=1 (requires Docker and built images)",
)

# Rendered canonically: RUN as an exec form with "<" escaped, the COPY heredoc
# as written, the RUN heredoc as an executable script.
TEXT = """USER root
RUN touch /usr/local/bin/tth-live-marker && echo $((1<<3)) > /usr/local/share/tth-live-shift
COPY --chmod=644 <<'EOT' /usr/local/share/
kept $HOME
EOT
RUN <<EOF
#!/bin/sh
echo from-script > /usr/local/share/tth-live-script
EOF"""


def test_derived_image_follows_its_base_and_superseded_images_are_removed(
    tmp_path: Path,
) -> None:
    import docker

    client: Any = docker.from_env()
    harness_image = f"tth-codex:{os.environ.get('TTH_SANDBOX_IMAGE_TAG', 'latest')}"
    client.images.get(harness_image)
    base_tag = f"tth-codex:live-{uuid4().hex[:12]}"
    created: list[str] = []

    def rebuild_base(generation: int) -> None:
        # Stand-in for deploy/build-splits.sh: same tag, new image id.
        subprocess.run(
            ["docker", "build", "-q", "-t", base_tag, "-"],
            input=f"FROM {harness_image}\nLABEL tth.live-generation={generation}\n",
            text=True,
            check=True,
            capture_output=True,
        )
        created.append(client.images.get(base_tag).id)

    scope = ScopeLayout("tth-scope-" + uuid4().hex[:24], tmp_path)
    scope.state.mkdir()
    scope.image_file.write_text(
        json.dumps(custom_images.image_record(HarnessKind.CODEX, base_tag, TEXT))
    )
    try:
        rebuild_base(1)
        with custom_images.ensure_derived_image(
            client,
            HarnessKind.CODEX,
            base_tag=base_tag,
            dockerfile=TEXT,
            state_root=tmp_path,
            timeout=600,
        ) as first:
            created.append(first)
            output = client.containers.run(
                first,
                entrypoint=["sh", "-c"],
                command=[
                    "id -un && test -f /usr/local/bin/tth-live-marker && pwd && cd /usr/local/share"
                    " && cat tth-live-shift EOT tth-live-script"
                ],
                network_disabled=True,
                remove=True,
            ).decode()
            # The trailer restored the split's user and working directory.
            assert output.split() == [
                "agent",
                "/app/service",
                "8",
                "kept",
                "$HOME",
                "from-script",
            ]
        assert client.images.get(first).labels["tth.base-id"] == created[0]

        rebuild_base(2)
        with custom_images.ensure_derived_image(
            client,
            HarnessKind.CODEX,
            base_tag=base_tag,
            dockerfile=TEXT,
            state_root=tmp_path,
            timeout=600,
        ) as second:
            created.append(second)
        assert second != first

        removed = custom_images.collect_garbage(
            client, scopes=[scope], state_root=tmp_path, superseded=False
        )
        # The image built on the replaced base goes; the current one stays.
        assert first in removed and second not in removed
        client.images.get(second)
    finally:
        for image in reversed(created):
            with suppress(Exception):
                client.images.remove(image, force=True)
