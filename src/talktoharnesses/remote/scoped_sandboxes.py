"""Resolve immutable policy bindings to isolated, reusable Docker instances."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError
from tth_types.harness import HarnessConfiguration

from talktoharnesses.remote.isolated_sandbox import IsolatedSandbox
from talktoharnesses.remote.sandbox import SandboxConfig, SandboxStore
from talktoharnesses.remote.sandbox_paths import repository_directory
from talktoharnesses.sandbox_policies import SandboxPolicyStore


class ScopedSandboxManager:
    def __init__(
        self,
        config: SandboxConfig,
        *,
        store: SandboxStore,
        policies: SandboxPolicyStore,
        state_root: Path | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.policies = policies
        self.state_root = (
            state_root
            or Path(
                os.environ.get("TTH_SANDBOX_STATE_DIR", "~/.local/state/talktoharnesses/sandboxes")
            ).expanduser()
        ).resolve()
        self.instances: dict[str, IsolatedSandbox] = {}

    async def for_configuration(self, configuration: HarnessConfiguration) -> IsolatedSandbox:
        ref = configuration.sandbox_policy
        if ref is None:
            raise DomainError(ErrorCode.SANDBOX_POLICY_REQUIRED, "Select a sandbox policy.")
        revision = await self.policies.resolve(ref.id, ref.revision)
        if configuration.kind not in revision.policy.providers:
            raise DomainError(ErrorCode.SANDBOX_POLICY_DENIED, "Provider is not permitted.")
        root = Path(revision.policy.project_root)
        try:
            paths = tuple(
                dict.fromkeys(
                    str(Path(path).resolve(strict=True))
                    for path in (configuration.working_directory, *configuration.workspace_roots)
                )
            )
        except OSError as exc:
            raise DomainError(
                ErrorCode.SANDBOX_PATH_NOT_MOUNTED, "A workspace path does not exist."
            ) from exc
        writable = {str(root)}
        repository = revision.repository_directory
        for raw in paths:
            path = Path(raw)
            if path.is_relative_to(root):
                continue
            if raw != paths[0] and any(
                path.is_relative_to(read) for read in revision.policy.read_only_roots
            ):
                continue
            common = await asyncio.to_thread(repository_directory, path)
            if repository is None or common != repository:
                raise DomainError(
                    ErrorCode.SANDBOX_PATH_NOT_MOUNTED,
                    "Workspace is not in this project or a linked worktree.",
                )
            writable.add(str(path))
        if repository and not Path(repository).is_relative_to(root):
            writable.add(repository)
        roots = tuple(sorted(writable))
        if any(
            Path(read).is_relative_to(write) or Path(write).is_relative_to(read)
            for read in revision.policy.read_only_roots
            for write in roots
        ):
            raise DomainError(
                ErrorCode.SANDBOX_POLICY_DENIED,
                "Read-only dependencies must be outside the writable project roots.",
            )
        allowed = tuple(Path(path).resolve() for path in self.config.mount_roots)
        for raw in (*roots, *revision.policy.read_only_roots):
            path = Path(raw)
            if not any(path.is_relative_to(parent) for parent in allowed):
                raise DomainError(
                    ErrorCode.SANDBOX_PATH_NOT_MOUNTED,
                    "Mount is outside operator-approved host roots.",
                )
            if self.state_root.is_relative_to(path) or path.is_relative_to(self.state_root):
                raise DomainError(
                    ErrorCode.SANDBOX_POLICY_DENIED, "Gateway state cannot be mounted."
                )
            if any(
                Path(source).resolve().is_relative_to(path)
                for source in self.config.auth_files.values()
            ):
                raise DomainError(
                    ErrorCode.SANDBOX_POLICY_DENIED, "Host credentials cannot be mounted."
                )
        identity = json.dumps(
            [ref.model_dump(mode="json"), configuration.kind.value, roots], sort_keys=True
        )
        name = "tth-scope-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        if name not in self.instances:
            self.instances[name] = IsolatedSandbox(
                self.config,
                store=self.store,
                revision=revision,
                name=name,
                roots=roots,
                state_root=self.state_root,
            )
        return self.instances[name]

    async def is_running(self, kind: HarnessKind) -> bool:
        # The background monitor must never create a project scope.
        return any([await instance.is_running(kind) for instance in self.instances.values()])
