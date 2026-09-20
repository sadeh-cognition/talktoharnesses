"""Adapter registry construction: every kind is driven by a remote split.

Each kind resolves to the same ``RemoteHarnessAdapter`` implementation. The
``SandboxManager`` provides its endpoint from a Docker sandbox it spawns on
demand and tracks in the sandbox store.
"""

from __future__ import annotations

from importlib.metadata import version as package_version

from tth_types.enums import HarnessKind
from tth_types.harness import HarnessConfiguration

from talktoharnesses.providers.registry import AdapterRegistry
from talktoharnesses.remote.adapter import RemoteHarnessAdapter
from talktoharnesses.remote.sandbox import SandboxManager
from talktoharnesses.remote.scoped_sandboxes import ScopedSandboxManager


async def running_sandbox_adapter(
    sandboxes: ScopedSandboxManager, configuration: HarnessConfiguration
) -> RemoteHarnessAdapter | None:
    """Create a probe adapter with no capability to prepare a sandbox."""
    endpoint = await sandboxes.running_endpoint(configuration)
    if endpoint is None:
        return None
    return RemoteHarnessAdapter(
        configuration.kind, endpoint, adapter_version=package_version("talktoharnesses")
    )


def build_remote_adapter_registry(
    sandboxes: SandboxManager | ScopedSandboxManager,
) -> AdapterRegistry:
    """Registry with a remote adapter factory for every harness kind."""
    adapter_version = package_version("talktoharnesses")
    registry = AdapterRegistry()
    for kind in HarnessKind:

        def _factory(k: HarnessKind = kind) -> RemoteHarnessAdapter:
            return RemoteHarnessAdapter(
                k,
                sandboxes,
                adapter_version=adapter_version,
            )

        registry.register(kind, _factory)
    return registry
