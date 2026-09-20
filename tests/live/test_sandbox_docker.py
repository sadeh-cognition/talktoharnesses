"""Opt-in policy gateway gate using real Docker networking and synthetic credentials.

TALKTOHARNESSES_SANDBOX_DOCKER=1 enables the test. Images are selected by
TTH_SANDBOX_IMAGE_TAG. No provider inference request is made.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from tth_types.sandbox import SandboxPolicy, SandboxPolicyRef, SandboxPolicyRevision

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.remote.isolated_sandbox import IsolatedSandbox
from talktoharnesses.remote.sandbox import SandboxConfig

pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_SANDBOX_DOCKER") != "1",
    reason="set TALKTOHARNESSES_SANDBOX_DOCKER=1 (requires Docker and built images)",
)


async def test_gateway_boot_reuse_network_denials_and_command_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import docker
    from docker.errors import NotFound

    root = tmp_path / "workspace"
    root.mkdir()
    auth = tmp_path / "original-login" / "auth.json"
    auth.parent.mkdir()
    auth.write_text('{"tokens":{"access_token":"test-only-original-token"}}')
    revision = SandboxPolicyRevision(
        ref=SandboxPolicyRef(id=uuid4(), revision=1),
        policy=SandboxPolicy(project_root=str(root)),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-provider-key")
    config = SandboxConfig.from_env({}).model_copy(
        update={
            "image_tag": os.environ.get("TTH_SANDBOX_IMAGE_TAG", "latest"),
            "mount_roots": (str(root),),
            "auth_files": {HarnessKind.CODEX: str(auth)},
            "workspace_setup_enabled": False,
        }
    )
    name = "tth-policy-test-" + uuid4().hex[:12]
    manager = IsolatedSandbox(
        config,
        store=None,
        revision=revision,
        name=name,
        roots=(str(root),),
        state_root=tmp_path / "private",
    )
    client = docker.from_env()
    try:
        endpoint = await manager.endpoint(HarnessKind.CODEX, (str(root),))
        assert await manager.endpoint(HarnessKind.CODEX) == endpoint
        async with httpx.AsyncClient(base_url=endpoint.base_url, timeout=10) as http:
            health = await http.get("/v1/health")
            assert health.status_code == 200 and health.json()["kind"] == "codex"
            denied_control = await http.post(
                "/v1/sessions", headers={"X-TTH-Split-Token": "wrong"}, json={}
            )
            assert denied_control.status_code == 403
        container = client.containers.get(name)
        # Reconcile a gateway left without its private attachment. Retrying
        # preparation must repair an existing container, not only fresh ones.
        gateway = client.containers.get(name + "-gateway")
        gateway.stop()
        network = client.networks.get(name + "-network")
        network.disconnect(gateway)
        assert endpoint.token is not None
        await asyncio.to_thread(manager._ensure_container, HarnessKind.CODEX, endpoint.token)  # pyright: ignore[reportPrivateUsage]
        await manager._wait_healthy(HarnessKind.CODEX, manager._base_url(HarnessKind.CODEX))  # pyright: ignore[reportPrivateUsage]
        gateway.reload()
        assert name + "-network" in gateway.attrs["NetworkSettings"]["Networks"]
        assert client.containers.get(name).id == container.id
        original_gateway_id = gateway.id
        replacement = tmp_path / "replacement-login" / auth.name
        replacement.parent.mkdir()
        replacement.write_text('{"tokens":{"access_token":"test-only-replacement-token"}}')
        manager.config = manager.config.model_copy(
            update={"auth_files": {HarnessKind.CODEX: str(replacement)}}
        )
        await asyncio.to_thread(manager._ensure_container, HarnessKind.CODEX, endpoint.token)  # pyright: ignore[reportPrivateUsage]
        await manager._wait_healthy(HarnessKind.CODEX, manager._base_url(HarnessKind.CODEX))  # pyright: ignore[reportPrivateUsage]
        gateway = client.containers.get(name + "-gateway")
        assert gateway.id != original_gateway_id
        assert client.containers.get(name).id == container.id
        credential = gateway.exec_run(
            [
                "python",
                "-c",
                "import json; "
                "print(json.load(open('/credentials/auth.json'))['tokens']['access_token'])",
            ]
        )
        assert credential.exit_code == 0
        assert isinstance(credential.output, bytes)
        assert credential.output.strip() == b"test-only-replacement-token"
        assert "test-only-provider-key" not in json.dumps(container.attrs)
        allowed = container.exec_run(
            ["curl", "-fsS", "--max-time", "20", "-o", "/dev/null", "https://pypi.org/simple/pip/"]
        )
        assert allowed.exit_code == 0, allowed.output
        denied = container.exec_run(
            ["curl", "-sS", "--max-time", "5", "https://unapproved.example/"]
        )
        assert denied.exit_code != 0 and b"403" in denied.output
        direct = container.exec_run(
            ["curl", "-sS", "--noproxy", "*", "--max-time", "3", "https://1.1.1.1/"]
        )
        assert direct.exit_code != 0
        command = container.exec_run(
            [
                "curl",
                "-fsS",
                "--max-time",
                "5",
                "-H",
                "Content-Type: application/json",
                "-d",
                json.dumps({"command": "git push", "cwd": str(root)}),
                "http://tth-gateway.invalid:8080/__tth/command-check",
            ]
        )
        assert command.exit_code == 0
        assert isinstance(command.output, bytes)
        assert json.loads(command.output)["allowed"] is False
    finally:
        for suffix in ("-gateway", ""):
            with suppress(NotFound):
                client.containers.get(name + suffix).remove(force=True)
        with suppress(NotFound):
            client.networks.get(name + "-network").remove()
        for suffix in ("-home", "-data"):
            with suppress(NotFound):
                client.volumes.get(name + suffix).remove()
        client.close()
