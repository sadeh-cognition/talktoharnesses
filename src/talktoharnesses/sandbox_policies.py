"""Persistence boundary for immutable project sandbox policies."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from tth_types.sandbox import SandboxPolicyRevision, SaveSandboxPolicy


class SandboxPolicyStore(Protocol):
    async def get(
        self, owner_id: str, policy_id: UUID, revision: int | None = None
    ) -> SandboxPolicyRevision: ...

    async def save(
        self, owner_id: str, policy_id: UUID, request: SaveSandboxPolicy
    ) -> SandboxPolicyRevision: ...

    async def resolve(self, policy_id: UUID, revision: int) -> SandboxPolicyRevision:
        """Runtime-only lookup of a binding already authorized by the service."""
        ...
