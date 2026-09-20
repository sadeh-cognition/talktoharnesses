import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError
from tth_types.harness import HarnessConfiguration
from tth_types.sandbox import SandboxPolicy, SaveSandboxPolicy

from talktoharnesses.django.sandbox_policies import DjangoSandboxPolicyStore
from talktoharnesses.django.sandbox_store import DjangoSandboxStore
from talktoharnesses.remote.sandbox import SandboxConfig
from talktoharnesses.remote.scoped_sandboxes import ScopedSandboxManager


@pytest.mark.django_db(transaction=True)
async def test_policy_scope_reuses_only_compatible_revisions_and_mounts(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "-b", "work", str(worktree)],
        check=True,
        capture_output=True,
    )
    policies = DjangoSandboxPolicyStore()
    revision = await policies.save(
        "owner",
        uuid4(),
        SaveSandboxPolicy(policy=SandboxPolicy(project_root=str(root)), expected_revision=0),
    )
    manager = ScopedSandboxManager(
        SandboxConfig(mount_roots=(str(tmp_path),)),
        store=DjangoSandboxStore(),
        policies=policies,
        state_root=tmp_path / "private",
    )
    configuration = HarnessConfiguration(
        kind=HarnessKind.CODEX, working_directory=str(root), sandbox_policy=revision.ref
    )
    first = await manager.for_configuration(configuration)
    assert await manager.for_configuration(configuration) is first
    linked = await manager.for_configuration(
        configuration.model_copy(update={"working_directory": str(worktree)})
    )
    assert linked is not first and str(worktree) in linked.roots
    latest = await policies.save(
        "owner", revision.ref.id, SaveSandboxPolicy(policy=revision.policy, expected_revision=1)
    )
    assert await manager.for_configuration(configuration) is first
    assert (
        await manager.for_configuration(
            configuration.model_copy(update={"sandbox_policy": latest.ref})
        )
        is not first
    )
    other_provider = await manager.for_configuration(
        configuration.model_copy(update={"kind": HarnessKind.CLAUDE})
    )
    assert other_provider.name != first.name


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "violation",
    ["missing", "provider", "workspace", "operator", "private", "credentials", "readonly"],
)
async def test_scope_admission_rejects_mount_and_policy_violations(
    tmp_path: Path, violation: str
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    policy = SandboxPolicy(project_root=str(root), providers=(HarnessKind.CODEX,))
    if violation == "readonly":
        policy = policy.model_copy(update={"read_only_roots": (str(root),)})
    policies = DjangoSandboxPolicyStore()
    revision = await policies.save(
        "owner", uuid4(), SaveSandboxPolicy(policy=policy, expected_revision=0)
    )
    config = SandboxConfig(
        mount_roots=(str(other if violation == "operator" else tmp_path),),
        auth_files={HarnessKind.CODEX: str(root / "auth.json")}
        if violation == "credentials"
        else {},
    )
    manager = ScopedSandboxManager(
        config,
        store=DjangoSandboxStore(),
        policies=policies,
        state_root=root / "state" if violation == "private" else tmp_path / "state",
    )
    configuration = HarnessConfiguration(
        kind=HarnessKind.CLAUDE if violation == "provider" else HarnessKind.CODEX,
        working_directory=str(other if violation == "workspace" else root),
        sandbox_policy=None if violation == "missing" else revision.ref,
    )
    with pytest.raises(DomainError) as failure:
        await manager.for_configuration(configuration)
    assert failure.value.code in {
        ErrorCode.SANDBOX_POLICY_REQUIRED,
        ErrorCode.SANDBOX_POLICY_DENIED,
        ErrorCode.SANDBOX_PATH_NOT_MOUNTED,
    }


@pytest.mark.django_db(transaction=True)
async def test_dependency_roots_remain_read_only_and_cannot_be_session_cwd(tmp_path: Path) -> None:
    root, dependency = tmp_path / "project", tmp_path / "dependency"
    root.mkdir()
    dependency.mkdir()
    policies = DjangoSandboxPolicyStore()
    revision = await policies.save(
        "owner",
        uuid4(),
        SaveSandboxPolicy(
            policy=SandboxPolicy(project_root=str(root), read_only_roots=(str(dependency),)),
            expected_revision=0,
        ),
    )
    manager = ScopedSandboxManager(
        SandboxConfig(mount_roots=(str(tmp_path),)),
        store=DjangoSandboxStore(),
        policies=policies,
        state_root=tmp_path / "private",
    )
    configuration = HarnessConfiguration(
        kind=HarnessKind.CODEX,
        working_directory=str(root),
        workspace_roots=(str(dependency),),
        sandbox_policy=revision.ref,
    )
    sandbox = await manager.for_configuration(configuration)
    assert str(dependency) not in sandbox.roots
    assert sandbox.revision.policy.read_only_roots == (str(dependency),)
    for cwd in (dependency, tmp_path / "missing"):
        with pytest.raises(DomainError) as failure:
            await manager.for_configuration(
                configuration.model_copy(update={"working_directory": str(cwd)})
            )
        assert failure.value.code is ErrorCode.SANDBOX_PATH_NOT_MOUNTED
