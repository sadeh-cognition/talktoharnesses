import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock, PropertyMock
from uuid import uuid4

import pytest
from docker.errors import APIError, ImageNotFound, NotFound
from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError, public_message
from tth_types.sandbox import SandboxPolicy, SandboxPolicyRef, SandboxPolicyRevision

from talktoharnesses.remote import docker_ops
from talktoharnesses.remote.isolated_sandbox import IsolatedSandbox
from talktoharnesses.remote.sandbox import SandboxConfig


@pytest.mark.parametrize(
    "failure", ["none", "attachment", "replacement", "credentials", "stopped_gateway"]
)
def test_launch_keeps_secrets_and_public_network_outside_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
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
    created: list[Any] = []

    def get(name: str) -> Any:
        if name not in containers:
            raise NotFound(name)
        return containers[name]

    def run(image: str, **kwargs: Any) -> Any:
        runs.append(kwargs)
        if image == manager.gateway_image:
            ca = manager.state / "ca" / "mitmproxy-ca-cert.pem"
            ca.parent.mkdir()
            ca.write_text("public-certificate")
        elif kwargs.get("name") == manager.name:
            container: Any = Mock()
            container.id = "sandbox-container"
            container.status = "running"
            container.image.tags = [image]
            container.attrs = {
                "Config": {
                    "Env": [f"{key}={value}" for key, value in kwargs["environment"].items()]
                },
                "Mounts": [
                    {
                        "Destination": mount["Target"],
                        "Source": "/daemon" + mount["Source"]
                        if mount["Type"] == "bind"
                        else mount["Source"],
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
            return container
        return b""

    def create(image: str, **kwargs: Any) -> Any:
        gateways.append(kwargs)
        container: Any = Mock()
        container.status = "created"
        # Resolving a container's image fails once a rebuild removed it; the
        # recorded id remains readable.
        type(container).image = PropertyMock(side_effect=ImageNotFound("removed"))
        container.attrs = {
            "Image": client.images.get.return_value.id,
            "NetworkSettings": {"Networks": {}, "Ports": {"8080/tcp": [{"HostPort": "19234"}]}},
        }
        container.start.side_effect = lambda: setattr(container, "status", "running")
        created.append(container)
        containers[manager.name + "-gateway"] = container
        return container

    client.containers.get.side_effect = get
    client.containers.run.side_effect = run
    client.containers.create.side_effect = create

    def connect(container: Any, *, aliases: list[str]) -> None:
        if failure == "attachment" and network.connect.call_count == 1:
            raise RuntimeError("network attachment failed")
        container.attrs["NetworkSettings"]["Networks"]["scope-network"] = {"Aliases": aliases}

    network.connect.side_effect = connect

    def docker_client(kind: HarnessKind) -> Any:
        return client

    monkeypatch.setattr(manager, "_docker_client", docker_client)
    relays: list[Path] = []
    monkeypatch.setattr("talktoharnesses.remote.isolated_sandbox.ensure_mcp_relay", relays.append)
    if failure == "attachment":
        with pytest.raises(RuntimeError, match="network attachment failed"):
            manager._ensure_container(HarnessKind.CODEX, "host-only-control")  # pyright: ignore[reportPrivateUsage]
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
    # Only a sandbox with MCP routes gets a host relay, including after a
    # proxy restart where no split has been configured yet.
    assert relays == []
    (manager.state / "mcp-routes.json").write_text("{}")
    # Reattachment preserves identities, permissions, and isolated homes.
    manager._ensure_container(HarnessKind.CODEX, "host-only-control")  # pyright: ignore[reportPrivateUsage]
    assert relays == [manager.state]
    # A gateway left unstarted by the failed attachment is recreated.
    assert len(gateways) == (2 if failure == "attachment" else 1)
    assert len([call for call in runs if call.get("name") == "scope"]) == 1
    assert network.connect.call_count == (2 if failure == "attachment" else 1)
    if failure == "stopped_gateway":
        # A Docker Desktop restart can leave a stopped gateway unstartable;
        # it holds no state, so it is recreated instead of restarted.
        created[0].status = "exited"
        manager._ensure_container(HarnessKind.CODEX, "host-only-control")  # pyright: ignore[reportPrivateUsage]
        created[0].remove.assert_called_once_with(force=True)
        created[0].start.assert_called_once()
        assert len(gateways) == 2 and created[1].status == "running"
        return
    assert "scope-network" in containers["scope-gateway"].attrs["NetworkSettings"]["Networks"]
    if failure == "replacement":
        previous = containers["scope-gateway"]
        previous.remove.side_effect = [RuntimeError("removal failed"), None]
        with pytest.raises(RuntimeError, match="removal failed"):
            manager._ensure_container(HarnessKind.CODEX, "new-control")  # pyright: ignore[reportPrivateUsage]
        assert json.loads((manager.state / "config.json").read_text()) == config
        manager._ensure_container(HarnessKind.CODEX, "new-control")  # pyright: ignore[reportPrivateUsage]
        assert previous.remove.call_count == 2
        assert len(gateways) == 2
        assert (
            json.loads((manager.state / "config.json").read_text())["control_token"]
            == "new-control"
        )
    if failure == "credentials":
        replacement_dir = tmp_path / "replacement-login"
        replacement_dir.mkdir()
        replacement_auth = replacement_dir / auth.name
        replacement_auth.write_text('{"tokens":{"access_token":"replacement-secret"}}')
        manager.config = manager.config.model_copy(
            update={"auth_files": {HarnessKind.CODEX: str(replacement_auth)}}
        )
        manager._ensure_container(HarnessKind.CODEX, "host-only-control")  # pyright: ignore[reportPrivateUsage]
        assert len(gateways) == 2
        assert len([call for call in runs if call.get("name") == "scope"]) == 1
        credential_mount = next(
            mount for mount in gateways[-1]["mounts"] if mount["Target"] == "/credentials"
        )
        assert credential_mount["Source"] == str(replacement_dir)
        assert json.loads((manager.state / "config.json").read_text())["auth_source"] == str(
            replacement_auth
        )
    # A rebuilt gateway image replaces the gateway even though the image the
    # running gateway was created from no longer exists.
    client.images.get.return_value.id = "sha256:rebuilt"
    previous = containers["scope-gateway"]
    previous.remove.side_effect = None
    previous.remove.reset_mock()
    manager._ensure_container(HarnessKind.CODEX, "host-only-control")  # pyright: ignore[reportPrivateUsage]
    previous.remove.assert_called_once_with(force=True)
    assert containers["scope-gateway"].attrs["Image"] == "sha256:rebuilt"
    # Recorded VM translations do not excuse a different bind source.
    bound = next(mount for mount in containers["scope"].attrs["Mounts"] if mount["Type"] == "bind")
    bound["Source"] = "/different-source"
    assert not manager._container_matches(  # pyright: ignore[reportPrivateUsage]
        containers["scope"],
        HarnessKind.CODEX,
        image="tth-codex:latest",
        name="scope",
        environment=sandbox["environment"],
    )


def _returning(client: Any) -> Any:
    def docker_client(kind: HarnessKind) -> Any:
        del kind
        return client

    return docker_client


def _image_present(kind: HarnessKind) -> None:
    del kind


def _docker_cli(kind: HarnessKind | None = None) -> str:
    del kind
    return "docker"


def _scope(tmp_path: Path) -> IsolatedSandbox:
    root = tmp_path / "project"
    root.mkdir()
    revision = SandboxPolicyRevision(
        ref=SandboxPolicyRef(id=uuid4(), revision=1),
        policy=SandboxPolicy(project_root=str(root)),
    )
    auth = tmp_path / "auth.json"
    auth.write_text('{"tokens":{"access_token":"real-secret"}}')
    return IsolatedSandbox(
        SandboxConfig(auth_files={HarnessKind.CODEX: str(auth)}),
        store=None,
        revision=revision,
        name="scope",
        roots=(str(root),),
        state_root=tmp_path / "private",
    )


@pytest.mark.parametrize(
    ("error_message", "reason"),
    [
        (
            "all predefined address pools have been fully subnetted",
            "network_pool_exhausted",
        ),
        ("driver failed: port is already allocated", "port_conflict"),
        ("invalid network options", "container_start_failed"),
    ],
)
async def test_docker_failures_become_sandbox_unavailable_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_message: str, reason: str
) -> None:
    manager = _scope(tmp_path)
    client: Any = Mock()
    client.networks.get.side_effect = NotFound("scope-network")
    client.networks.create.side_effect = APIError(error_message)
    monkeypatch.setattr(manager, "_docker_client", _returning(client))
    monkeypatch.setattr(manager, "_ensure_image", _image_present)

    with pytest.raises(DomainError) as excinfo:
        await manager._prepare(HarnessKind.CODEX)  # pyright: ignore[reportPrivateUsage]

    assert excinfo.value.code is ErrorCode.SANDBOX_UNAVAILABLE
    assert excinfo.value.details == {"kind": "codex", "reason": reason}


def test_exhausted_network_pools_have_an_actionable_public_message() -> None:
    message = public_message(
        ErrorCode.SANDBOX_UNAVAILABLE, details={"reason": "network_pool_exhausted"}
    )

    assert "no free network address ranges" in message
    assert "subnetted" not in message


def test_failed_gateway_image_build_logs_its_output_and_keeps_it_from_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manager = _scope(tmp_path)
    client: Any = Mock()
    client.images.get.side_effect = ImageNotFound("tth-policy-gateway")
    monkeypatch.setattr(manager, "_docker_client", _returning(client))
    monkeypatch.setattr(docker_ops, "ensure_docker_cli_available", _docker_cli)

    def failing_build(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="private output")

    monkeypatch.setattr("talktoharnesses.remote.docker_ops.subprocess.run", failing_build)

    with pytest.raises(DomainError) as excinfo:
        manager._ensure_container(HarnessKind.CODEX, "control")  # pyright: ignore[reportPrivateUsage]

    failure = excinfo.value
    assert failure.code is ErrorCode.SANDBOX_UNAVAILABLE
    assert failure.details == {
        "kind": "codex",
        "reason": "image_build_failed",
        "build_tail": "private output",
    }
    assert "private output" not in failure.message
    assert "private output" not in public_message(failure.code, details=failure.details)
    # The operator reads the build output in the server log.
    assert "private output" in caplog.text
