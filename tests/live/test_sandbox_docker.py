"""Opt-in Docker sandbox gate: SandboxManager boots a real split container.

Requires Docker; the tth-claude image is built on demand if absent
(pre-build with deploy/build-splits.sh claude to skip the build wait).
Enable with TALKTOHARNESSES_SANDBOX_DOCKER=1.
"""

from __future__ import annotations

import os

import httpx
import pytest

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.remote.sandbox import SandboxConfig, SandboxManager

pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_SANDBOX_DOCKER") != "1",
    reason="set TALKTOHARNESSES_SANDBOX_DOCKER=1 (requires Docker + built tth-claude image)",
)


async def test_sandbox_boot_health_and_reuse() -> None:
    # A generous prepare grace keeps a cold image build inside the first call.
    config = SandboxConfig.from_env({}).model_copy(update={"prepare_grace": 1800.0})
    manager = SandboxManager(config)

    endpoint = await manager.endpoint(HarnessKind.CLAUDE)
    assert endpoint.base_url == f"http://127.0.0.1:{config.ports[HarnessKind.CLAUDE]}"
    assert endpoint.token, "sandbox boot must inject a split token"

    async with httpx.AsyncClient(base_url=endpoint.base_url, timeout=5.0) as client:
        health = await client.get("/v1/health")
        assert health.status_code == 200
        assert health.json()["kind"] == "claude"

    # Second call reuses the running container without re-checking Docker.
    again = await manager.endpoint(HarnessKind.CLAUDE)
    assert again.base_url == endpoint.base_url
    assert again.token == endpoint.token
