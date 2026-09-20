from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from tests.runtime.memory_persistence import MemoryPersistence
from tth_types.enums import HarnessKind
from tth_types.harness import HarnessCapabilities, HarnessConfiguration, LaunchSnapshot
from tth_types.sandbox import SandboxPolicy, SaveSandboxPolicy
from tth_types.split_api import ProbeResponse

from talktoharnesses.application.readiness import ReadinessProbeMonitor
from talktoharnesses.django.sandbox_policies import DjangoSandboxPolicyStore
from talktoharnesses.django.sandbox_store import DjangoSandboxStore
from talktoharnesses.domain.models import HarnessInstance
from talktoharnesses.remote.isolated_sandbox import IsolatedSandbox
from talktoharnesses.remote.registry import build_remote_adapter_registry, running_sandbox_adapter
from talktoharnesses.remote.sandbox import SandboxConfig, SandboxRecordData
from talktoharnesses.remote.scoped_sandboxes import ScopedSandboxManager


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("availability", ["ready", "unhealthy", "disappeared"])
async def test_restart_readiness_never_enters_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, availability: str
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    policies = DjangoSandboxPolicyStore()
    revision = await policies.save(
        "owner",
        uuid4(),
        SaveSandboxPolicy(policy=SandboxPolicy(project_root=str(root)), expected_revision=0),
    )
    store = DjangoSandboxStore()
    manager = ScopedSandboxManager(
        SandboxConfig(image_tag="new-deployment", mount_roots=(str(root),)),
        store=store,
        policies=policies,
        state_root=tmp_path / "private",
    )
    configuration = HarnessConfiguration(
        kind=HarnessKind.CODEX, working_directory=str(root), sandbox_policy=revision.ref
    )
    sandbox = await manager.for_configuration(configuration)
    now = datetime.now(UTC)
    record = SandboxRecordData(
        kind=HarnessKind.CODEX,
        scope=sandbox.name,
        container_name=sandbox.name,
        image="tth-codex:previous-deployment",
        host_port=1234,
        base_url="http://127.0.0.1:1234/split",
        split_token="host-token",
        status="ready",
        created_at=now,
        updated_at=now,
    )
    await store.upsert(record)

    def container_running(instance: IsolatedSandbox, name: str) -> bool:
        return name in {instance.name, instance.name + "-gateway"}

    monkeypatch.setattr(IsolatedSandbox, "_container_running", container_running)
    prepare = Mock(side_effect=AssertionError("Readiness must not prepare containers"))
    monkeypatch.setattr(IsolatedSandbox, "_prepare", prepare)
    requests: list[httpx.Request] = []
    clients: list[httpx.AsyncClient] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == record.base_url + "/v1/probe"
        assert request.headers["X-TTH-Split-Token"] == record.split_token
        if availability == "disappeared":
            raise httpx.ConnectError("Gateway stopped after lookup", request=request)
        if availability == "unhealthy":
            return httpx.Response(503)
        capabilities = HarnessCapabilities(kind=HarnessKind.CODEX, version="1.2.3")
        return httpx.Response(
            200,
            content=ProbeResponse(
                capabilities=capabilities,
                launch=LaunchSnapshot(
                    harness_version="1.2.3",
                    working_directory=str(root),
                    adapter_version="split-1",
                    capabilities=capabilities,
                ),
            ).model_dump_json(),
        )

    class ProbeClient(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=httpx.MockTransport(respond), **kwargs)
            clients.append(self)

    monkeypatch.setattr(httpx, "AsyncClient", ProbeClient)
    manager.instances.clear()
    persistence = MemoryPersistence()
    harness = await persistence.create_harness(
        HarnessInstance(
            owner_id="owner",
            name="existing",
            kind=HarnessKind.CODEX,
            configuration=configuration,
            created_at=now,
        )
    )
    monitor = ReadinessProbeMonitor(
        persistence,
        build_remote_adapter_registry(manager),
        lambda: now,
        adapter_factory=partial(running_sandbox_adapter, manager),
    )
    assert await monitor._probe_one(harness) == (availability == "ready")  # pyright: ignore[reportPrivateUsage]
    prepare.assert_not_called()
    assert len(requests) == 1
    assert all(client.is_closed for client in clients)
    assert await store.get(sandbox.name) == record
