import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from docker.errors import NotFound
from tth_types.enums import HarnessKind
from tth_types.sandbox import SandboxPolicy, SandboxPolicyRef, SandboxPolicyRevision

from talktoharnesses.remote.isolated_sandbox import IsolatedSandbox
from talktoharnesses.remote.sandbox import SandboxConfig


def test_launch_keeps_secrets_and_public_network_outside_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    dependency = tmp_path / "dependency"
    dependency.mkdir()
    auth = tmp_path / "auth.json"
    auth.write_text('{"tokens":{"access_token":"real-secret"}}')
    revision = SandboxPolicyRevision(
        ref=SandboxPolicyRef(id=uuid4(), revision=1),
        policy=SandboxPolicy(project_root=str(root), read_only_roots=(str(dependency),)),
    )
    manager = IsolatedSandbox(
        SandboxConfig(auth_files={HarnessKind.CODEX: str(auth)}),
        store=None,
        revision=revision,
        name="scope",
        roots=(str(root),),
        state_root=tmp_path / "private",
    )
    client: Any = Mock()
    network: Any = Mock()
    network.attrs = {
        "Internal": True,
        "Options": {
            "com.docker.network.bridge.gateway_mode_ipv4": "isolated",
            "com.docker.network.bridge.gateway_mode_ipv6": "isolated",
        },
    }
    client.networks.get.return_value = network
    containers: dict[str, Any] = {}
    runs: list[dict[str, Any]] = []
    gateways: list[dict[str, Any]] = []

    def get(name: str) -> Any:
        if name not in containers:
            raise NotFound(name)
        return containers[name]

    def run(image: str, **kwargs: Any) -> bytes:
        runs.append(kwargs)
        if image == manager.gateway_image:
            ca = manager.state / "ca" / "mitmproxy-ca-cert.pem"
            ca.parent.mkdir()
            ca.write_text("public-certificate")
        elif kwargs.get("name") == manager.name:
            container: Any = Mock()
            container.status = "running"
            container.image.tags = [image]
            container.attrs = {
                "Config": {
                    "Env": [f"{key}={value}" for key, value in kwargs["environment"].items()]
                },
                "Mounts": [
                    {
                        "Destination": mount["Target"],
                        "Source": mount["Source"],
                        "Type": mount["Type"],
                        "RW": not mount.get("ReadOnly", False),
                        "Name": mount["Source"],
                    }
                    for mount in kwargs["mounts"]
                ],
                "NetworkSettings": {"Networks": {"scope-network": {"IPAddress": "172.20.0.2"}}},
                "HostConfig": {
                    "CapDrop": kwargs["cap_drop"],
                    "SecurityOpt": kwargs["security_opt"],
                    "PidsLimit": kwargs["pids_limit"],
                    "Dns": kwargs["dns"],
                },
            }
            containers[manager.name] = container
        return b""

    def create(image: str, **kwargs: Any) -> Any:
        gateways.append(kwargs)
        container: Any = Mock()
        container.status = "created"
        container.image.id = client.images.get.return_value.id
        container.attrs = {"NetworkSettings": {"Ports": {"8080/tcp": [{"HostPort": "19234"}]}}}
        containers[manager.name + "-gateway"] = container
        return container

    client.containers.get.side_effect = get
    client.containers.run.side_effect = run
    client.containers.create.side_effect = create

    def docker_client(kind: HarnessKind) -> Any:
        return client

    monkeypatch.setattr(manager, "_docker_client", docker_client)
    manager._ensure_container(HarnessKind.CODEX, "host-only-control")  # pyright: ignore[reportPrivateUsage]
    sandbox = next(call for call in runs if call.get("name") == "scope")
    assert "real-secret" not in json.dumps(sandbox)
    assert "host-only-control" not in json.dumps(sandbox)
    assert sandbox["network"] == "scope-network" and sandbox["dns"] == ["127.0.0.1"]
    assert "ports" not in sandbox
    assert "real-secret" not in (manager.state / "virtual-auth.json").read_text()
    assert "real-secret" not in containers["scope"].attrs["Config"]["Env"]
    assert gateways[0]["network"] == "bridge"
    assert gateways[0]["sysctls"]["net.ipv4.ip_forward"] == "0"
    assert network.connect.call_args.kwargs["aliases"] == ["tth-gateway.invalid"]
    config = json.loads((manager.state / "config.json").read_text())
    assert config["control_token"] == "host-only-control"
    assert config["auth_file"] == "/credentials/auth.json"
    assert config["split_token"] != config["control_token"]
    assert manager.gateway_port == 19234
    # Reattachment preserves identities, permissions, and isolated homes.
    manager._ensure_container(HarnessKind.CODEX, "host-only-control")  # pyright: ignore[reportPrivateUsage]
    assert len(gateways) == 1
    assert len([call for call in runs if call.get("name") == "scope"]) == 1
