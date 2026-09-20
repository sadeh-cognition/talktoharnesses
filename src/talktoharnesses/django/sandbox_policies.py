"""Owner-scoped policy writes with optimistic concurrency and immutable revisions."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from talktoharnesses.domain.transitions import ConversationState
from pathlib import Path

from asgiref.sync import sync_to_async
from django.db import transaction
from tth_types.enums import ErrorCode
from tth_types.errors import DomainError
from tth_types.sandbox import (
    SandboxPolicy,
    SandboxPolicyRef,
    SandboxPolicyRevision,
    SaveSandboxPolicy,
)

from talktoharnesses.django.models import SandboxPolicyRecord, SandboxPolicyRevisionRecord
from talktoharnesses.remote.sandbox_paths import repository_directory


class DjangoSandboxPolicyStore:
    async def get(
        self, owner_id: str, policy_id: UUID, revision: int | None = None
    ) -> SandboxPolicyRevision:
        return await sync_to_async(self._get, thread_sensitive=True)(owner_id, policy_id, revision)

    def _get(self, owner_id: str, policy_id: UUID, revision: int | None) -> SandboxPolicyRevision:
        policy = SandboxPolicyRecord.objects.filter(pk=policy_id, owner_id=owner_id).first()
        if policy is None:
            raise DomainError(ErrorCode.NOT_FOUND, "Sandbox policy not found.")
        return self.resolve_sync(policy_id, revision or policy.latest_revision)

    async def resolve(self, policy_id: UUID, revision: int) -> SandboxPolicyRevision:
        return await sync_to_async(self.resolve_sync, thread_sensitive=True)(policy_id, revision)

    def resolve_sync(self, policy_id: UUID, revision: int) -> SandboxPolicyRevision:
        row = SandboxPolicyRevisionRecord.objects.filter(
            policy_id=policy_id, revision=revision
        ).first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "Sandbox policy revision not found.")
        return SandboxPolicyRevision(
            ref=SandboxPolicyRef(id=policy_id, revision=revision),
            policy=SandboxPolicy.model_validate(row.rules),
            repository_directory=row.repository_directory,
        )

    async def save(
        self, owner_id: str, policy_id: UUID, request: SaveSandboxPolicy
    ) -> SandboxPolicyRevision:
        return await sync_to_async(self._save, thread_sensitive=True)(owner_id, policy_id, request)

    @transaction.atomic
    def _save(
        self, owner_id: str, policy_id: UUID, request: SaveSandboxPolicy
    ) -> SandboxPolicyRevision:
        policy, _ = SandboxPolicyRecord.objects.get_or_create(
            pk=policy_id, defaults={"owner_id": owner_id}
        )
        if policy.owner_id != owner_id:
            raise DomainError(ErrorCode.NOT_FOUND, "Sandbox policy not found.")
        changed = SandboxPolicyRecord.objects.filter(
            pk=policy_id, latest_revision=request.expected_revision
        ).update(latest_revision=request.expected_revision + 1)
        if not changed:
            raise DomainError(ErrorCode.OPTIMISTIC_CONFLICT, "Sandbox policy changed; reload it.")
        revision = request.expected_revision + 1
        try:
            root = Path(request.policy.project_root).resolve(strict=True)
            read_only_roots = tuple(
                str(Path(path).resolve(strict=True)) for path in request.policy.read_only_roots
            )
        except OSError as exc:
            raise DomainError(
                ErrorCode.SANDBOX_POLICY_DENIED, "A sandbox mount path does not exist."
            ) from exc
        if not root.is_dir():
            raise DomainError(ErrorCode.SANDBOX_POLICY_DENIED, "Project root is not a directory.")
        normalized = request.policy.model_copy(
            update={
                "project_root": str(root),
                "read_only_roots": read_only_roots,
            }
        )
        repository = repository_directory(root)
        SandboxPolicyRevisionRecord.objects.create(
            policy=policy,
            revision=revision,
            rules=normalized.model_dump(mode="json"),
            repository_directory=repository,
        )
        return SandboxPolicyRevision(
            ref=SandboxPolicyRef(id=policy_id, revision=revision),
            policy=normalized,
            repository_directory=repository,
        )


def approval_is_blocked(state: ConversationState, interaction_id: UUID) -> bool:
    from tth_types.harness import ApprovalRequestPayload, CommandApprovalAction

    from talktoharnesses.command_policy import check_argv

    binding = state.binding
    interaction = state.interactions.get(interaction_id)
    if binding is None or binding.configuration.sandbox_policy is None or interaction is None:
        return False
    request = interaction.request
    if not isinstance(request, ApprovalRequestPayload):
        return False
    argv = (
        request.action.argv
        if isinstance(request.action, CommandApprovalAction)
        else request.command_args
    )
    if argv is None:
        return False
    ref = binding.configuration.sandbox_policy
    revision = DjangoSandboxPolicyStore().resolve_sync(ref.id, ref.revision)
    return not check_argv(
        argv, binding.configuration.working_directory, revision.policy.command_rules
    ).allowed
