"""Docker mechanics for a single immutable project policy and mount set."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError
from tth_types.sandbox import SandboxPolicyRevision

from talktoharnesses.gateway.credentials import CredentialVault, UnsupportedCredential, atomic_json
from talktoharnesses.gateway.routes import GATEWAY_HOST
from talktoharnesses.remote import sandbox_auth, sandbox_rtk
from talktoharnesses.remote.sandbox import (
    SandboxConfig,
    SandboxManager,
    SandboxStore,
    pids_limit,
    security_options,
)
from talktoharnesses.remote.sandbox_workspace import TOOLCHAIN_ENV

GATEWAY_IMAGE = "tth-policy-gateway"
CA_MOUNT_PATHS = ("/etc/tth/ca.pem", "/etc/ssl/certs/ca-certificates.crt")


class IsolatedSandbox(SandboxManager):
    def __init__(
        self,
        config: SandboxConfig,
        *,
        store: SandboxStore | None,
        revision: SandboxPolicyRevision,
        name: str,
        roots: tuple[str, ...],
        state_root: Path,
    ) -> None:
        super().__init__(config, store=store)
        self.revision = revision
        self.name = name
        self.roots = roots
        self.state = state_root / name
        self.gateway_port = 0
        self.gateway_image = f"{GATEWAY_IMAGE}:{config.image_tag}"

    def _container_name(self, kind: HarnessKind) -> str:
        return self.name

    def _image(self, kind: HarnessKind) -> str:
        return f"tth-{kind.value.replace('_', '-')}:{self.config.image_tag}"

    def _mount_roots(self) -> tuple[str, ...]:
        return (*self.roots, *self.revision.policy.read_only_roots)

    def _port_for(self, kind: HarnessKind) -> int:
        return self.gateway_port

    def _base_url(self, kind: HarnessKind) -> str:
        return f"http://127.0.0.1:{self.gateway_port}/split"

    def _ensure_gateway_image(self, client: Any) -> None:
        from docker.errors import ImageNotFound

        try:
            client.images.get(self.gateway_image)
        except ImageNotFound:
            root = Path(__file__).resolve().parents[3]
            subprocess.run(
                [
                    "docker",
                    "build",
                    "-f",
                    "deploy/gateway.Dockerfile",
                    "-t",
                    self.gateway_image,
                    "--build-arg",
                    f"UID={os.getuid()}",
                    "--build-arg",
                    f"GID={os.getgid()}",
                    ".",
                ],
                cwd=root,
                check=True,
                capture_output=True,
                timeout=self.config.build_timeout,
            )

    def _ensure_container(self, kind: HarnessKind, token: str) -> None:
        import fcntl

        from docker.errors import NotFound
        from docker.types import Mount

        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.state / "prepare.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            client = self._docker_client(kind)
            self._ensure_gateway_image(client)
            identity_path = self.state / "identity.json"
            if not identity_path.exists():
                atomic_json(
                    identity_path,
                    {"seed": secrets.token_hex(32), "split_token": secrets.token_urlsafe(32)},
                )
            identity = json.loads(identity_path.read_text())
            auth_file = self.config.auth_files.get(kind)
            keys = {
                key: os.environ[key]
                for key in self.config.env_passthrough.get(kind, ())
                if key in os.environ
            }
            # Existing operator-only split test/embedding override is a Python
            # import path, not a credential. All credential sources stay proxied.
            adapter_factory = keys.pop("TTH_SPLIT_ADAPTER_FACTORY", None)
            vault = CredentialVault(
                kind=kind,
                seed=identity["seed"],
                auth_file=Path(auth_file) if auth_file else None,
                api_keys=keys,
            )
            try:
                virtual_env, virtual_auth = vault.snapshot()
            except (OSError, ValueError, UnsupportedCredential) as exc:
                raise DomainError(
                    ErrorCode.CREDENTIAL_PROXY_UNSUPPORTED,
                    "The configured login cannot be proxied safely.",
                ) from exc
            network_name = self.name + "-network"
            try:
                network = client.networks.get(network_name)
            except NotFound:
                network = client.networks.create(
                    network_name,
                    driver="bridge",
                    internal=True,
                    options={
                        "com.docker.network.bridge.gateway_mode_ipv4": "isolated",
                        "com.docker.network.bridge.gateway_mode_ipv6": "isolated",
                    },
                    labels={"tth.sandbox": self.name},
                )
            network.reload()
            if not network.attrs.get("Internal") or any(
                network.attrs.get("Options", {}).get(
                    "com.docker.network.bridge.gateway_mode_" + version
                )
                != "isolated"
                for version in ("ipv4", "ipv6")
            ):
                raise DomainError(
                    ErrorCode.SANDBOX_POLICY_DENIED, "Sandbox network is not internal."
                )
            ca_file = self.state / "ca" / "mitmproxy-ca-cert.pem"
            if not ca_file.exists():
                client.containers.run(
                    self.gateway_image,
                    entrypoint=["python", "-c"],
                    command=[
                        "from mitmproxy.certs import CertStore; "
                        "CertStore.from_store('/state/ca', 'mitmproxy', 2048)"
                    ],
                    mounts=[Mount(target="/state", source=str(self.state), type="bind")],
                    network_disabled=True,
                    cap_drop=["ALL"],
                    security_opt=["no-new-privileges:true"],
                    remove=True,
                )
            certificate = self.state / "ca-public.pem"
            shutil.copyfile(ca_file, certificate)
            certificate.chmod(0o644)
            image = self._image(kind)
            if virtual_auth is not None:
                virtual_file = self.state / "virtual-auth.json"
                atomic_json(virtual_file, virtual_auth)
                sandbox_auth.seed_auth_file(
                    client,
                    Mount,
                    kind=kind,
                    auth_file=str(virtual_file),
                    image=image,
                    home_volume=self.name + "-home",
                )
            sandbox_rtk.seed_rtk_config(
                client, Mount, kind=kind, image=image, home_volume=self.name + "-home"
            )
            environment = {
                **TOOLCHAIN_ENV,
                **virtual_env,
                "TTH_SPLIT_TOKEN": identity["split_token"],
                "HTTPS_PROXY": f"http://{GATEWAY_HOST}:8080",
                "https_proxy": f"http://{GATEWAY_HOST}:8080",
                "NO_PROXY": "localhost,127.0.0.1,::1",
                "no_proxy": "localhost,127.0.0.1,::1",
                "NODE_USE_ENV_PROXY": "1",
                "NODE_EXTRA_CA_CERTS": "/etc/tth/ca.pem",
                "npm_config_proxy": f"http://{GATEWAY_HOST}:8080",
                "npm_config_https_proxy": f"http://{GATEWAY_HOST}:8080",
                "npm_config_cafile": "/etc/tth/ca.pem",
                "SSL_CERT_FILE": "/etc/tth/ca.pem",
                "REQUESTS_CA_BUNDLE": "/etc/tth/ca.pem",
                "UV_NATIVE_TLS": "true",
                "OTEL_SDK_DISABLED": "true",
                "NODE_NO_WARNINGS": "1",
                "TTH_COMMAND_CHECK_URL": f"http://{GATEWAY_HOST}:8080/__tth/command-check",
            }
            if adapter_factory:
                environment["TTH_SPLIT_ADAPTER_FACTORY"] = adapter_factory
            self._reconcile_container(
                client,
                Mount,
                NotFound,
                kind=kind,
                name=self.name,
                image=image,
                environment=environment,
                token=identity["split_token"],
            )
            sandbox = client.containers.get(self.name)
            sandbox.reload()
            address = sandbox.attrs["NetworkSettings"]["Networks"][network_name]["IPAddress"]
            gateway_mounts = [Mount(target="/state", source=str(self.state), type="bind")]
            gateway_auth = None
            if auth_file:
                source = Path(auth_file).resolve(strict=True)
                gateway_mounts.append(
                    Mount(target="/credentials", source=str(source.parent), type="bind")
                )
                gateway_auth = "/credentials/" + source.name
            gateway_config = {
                "revision": self.revision.model_dump(mode="json"),
                "kind": kind.value,
                "seed": identity["seed"],
                "control_token": token,
                "split_token": identity["split_token"],
                "split_address": address,
                "auth_file": gateway_auth,
                "api_keys": keys,
            }
            config_path = self.state / "config.json"
            config_changed = (
                not config_path.exists() or json.loads(config_path.read_text()) != gateway_config
            )
            atomic_json(config_path, gateway_config)
            gateway_name = self.name + "-gateway"
            try:
                gateway = client.containers.get(gateway_name)
                if config_changed or gateway.image.id != client.images.get(self.gateway_image).id:
                    gateway.remove(force=True)
                    gateway = None
            except NotFound:
                gateway = None
            if gateway is None:
                gateway = client.containers.create(
                    self.gateway_image,
                    name=gateway_name,
                    mounts=gateway_mounts,
                    network="bridge",
                    ports={"8080/tcp": ("127.0.0.1", None)},
                    cap_drop=["ALL"],
                    security_opt=["no-new-privileges:true"],
                    sysctls={"net.ipv4.ip_forward": "0", "net.ipv6.conf.all.forwarding": "0"},
                    mem_limit="512m",
                    pids_limit=128,
                    restart_policy={"Name": "unless-stopped"},
                    labels={"tth.sandbox": self.name},
                )
                network.connect(gateway, aliases=[GATEWAY_HOST])
            gateway.reload()
            if gateway.status != "running":
                gateway.start()
            gateway.reload()
            self.gateway_port = int(
                gateway.attrs["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostPort"]
            )

    def _container_matches(
        self,
        container: Any,
        kind: HarnessKind,
        *,
        image: str,
        name: str,
        environment: dict[str, str],
    ) -> bool:
        attrs = container.attrs
        actual_env = dict(item.split("=", 1) for item in attrs["Config"]["Env"] if "=" in item)
        mounts = {mount["Destination"]: mount for mount in attrs.get("Mounts", [])}
        expected_paths = {
            *self.roots,
            *self.revision.policy.read_only_roots,
            "/home/agent",
            "/data",
            *CA_MOUNT_PATHS,
        }
        return (
            image in (container.image.tags or [])
            and set(mounts) == expected_paths
            and all(actual_env.get(key) == value for key, value in environment.items())
            and set(attrs["NetworkSettings"]["Networks"]) == {name + "-network"}
            and set(attrs["HostConfig"].get("CapDrop", [])) == {"ALL"}
            and set(attrs["HostConfig"].get("SecurityOpt", [])) == set(security_options(kind))
            and attrs["HostConfig"].get("PidsLimit") == pids_limit(kind)
            and attrs["HostConfig"].get("Dns") == ["127.0.0.1"]
            and not attrs["HostConfig"].get("PortBindings")
            and all(
                mounts[root]["Type"] == "bind"
                and mounts[root]["Source"] == root
                and mounts[root]["RW"] == (root in self.roots)
                for root in (*self.roots, *self.revision.policy.read_only_roots)
            )
            and all(
                mounts[target].get("Name") == name + suffix
                for target, suffix in (("/home/agent", "-home"), ("/data", "-data"))
            )
            and all(
                mounts[target]["Source"] == str(self.state / "ca-public.pem")
                and not mounts[target]["RW"]
                for target in CA_MOUNT_PATHS
            )
        )

    def _create_container(
        self,
        client: Any,
        mount_type: Any,
        *,
        kind: HarnessKind,
        name: str,
        image: str,
        environment: dict[str, str],
    ) -> None:
        mounts = [
            mount_type(target="/home/agent", source=name + "-home", type="volume"),
            mount_type(target="/data", source=name + "-data", type="volume"),
            *(
                mount_type(
                    target=target,
                    source=str(self.state / "ca-public.pem"),
                    type="bind",
                    read_only=True,
                )
                for target in CA_MOUNT_PATHS
            ),
            *(mount_type(target=root, source=root, type="bind") for root in self.roots),
            *(
                mount_type(target=root, source=root, type="bind", read_only=True)
                for root in self.revision.policy.read_only_roots
            ),
        ]
        client.containers.run(
            image,
            name=name,
            detach=True,
            init=True,
            mounts=mounts,
            environment=environment,
            network=name + "-network",
            dns=["127.0.0.1"],
            cap_drop=["ALL"],
            security_opt=security_options(kind),
            pids_limit=pids_limit(kind),
            mem_limit="4g",
            restart_policy={"Name": "unless-stopped"},
            labels={"tth.sandbox": name},
        )
