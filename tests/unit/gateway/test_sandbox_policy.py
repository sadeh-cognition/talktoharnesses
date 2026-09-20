import base64
import json
import time
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError
from tth_types.sandbox import (
    CommandCheck,
    CommandRule,
    EgressRule,
    SandboxPolicy,
    SaveSandboxPolicy,
)

from talktoharnesses.command_policy import check_command
from talktoharnesses.django.sandbox_policies import DjangoSandboxPolicyStore
from talktoharnesses.gateway.credentials import CredentialVault, UnsupportedCredential, atomic_json
from talktoharnesses.gateway.routes import META_GATEWAY_BASE, permitted_request, public_address


@pytest.mark.parametrize(
    "command",
    [
        "git push origin feature/x",
        "env X=y /usr/bin/git -C /repo push",
        "echo hi && sudo id",
        "echo $(git push)",
        "git status | bash -lc 'git reset --hard'",
        "rm -rf /",
        "rm -rf $HOME",
        "rm -rf ~",
        "rm -rf /home/agent/*",
        "git clean -xfd",
        "npm publish",
        "gh pr create",
        "rtk git push",
        "mount /dev/sda /mnt",
        "cat <(git push)",
    ],
)
def test_blocks_compound_and_wrapped_commands(command: str) -> None:
    decision = check_command(CommandCheck(command=command, cwd="/repo"))
    assert not decision.allowed and decision.reason


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git diff --stat",
        "git reset --soft HEAD~1",
        "rm -rf build",
        "uv run pytest",
        "printf 'git push'",
        "echo hello | cat",
    ],
)
def test_permits_development_commands(command: str) -> None:
    assert check_command(CommandCheck(command=command, cwd="/repo")).allowed


def test_project_rule_and_unsupported_shell_syntax_fail_closed() -> None:
    rules = (CommandRule(argv=("curl", "--upload-file")),)
    assert not check_command(
        CommandCheck(command="curl --upload-file file https://x.example", cwd="/repo"), rules
    ).allowed
    assert not check_command(CommandCheck(command="echo '", cwd="/repo")).allowed


def test_egress_requires_exact_host_method_path_and_public_address() -> None:
    policy = SandboxPolicy(
        project_root="/repo", egress=(EgressRule(host="forge.example", path="/repo"),)
    )
    assert permitted_request(
        policy, "forge.example", "/repo/info/refs?service=git-upload-pack", "GET"
    )
    for host, path, method in [
        ("evilforge.example", "/repo", "GET"),
        ("forge.example", "/repository", "GET"),
        ("forge.example", "/repo", "POST"),
        ("forge.example", "/repo/../admin", "GET"),
        ("forge.example", "/repo/%252e%252e/admin", "GET"),
        ("forge.example", "/repo/info/refs?service=git-receive-pack", "GET"),
    ]:
        assert not permitted_request(policy, host, path, method)
    for address in (
        "127.0.0.1",
        "169.254.169.254",
        "10.0.0.1",
        "::1",
        "::ffff:127.0.0.1",
        "224.0.0.1",
    ):
        assert not public_address(address)
    assert public_address("8.8.8.8")
    with pytest.raises(ValidationError):
        EgressRule(host="*.example.com")


def test_credentials_are_scoped_and_refresh_is_persisted_without_disclosure(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    atomic_json(
        path,
        {
            "tokens": {
                "access_token": "real-access",
                "refresh_token": "real-refresh",
                "account_id": "account",
            }
        },
    )
    vault = CredentialVault(kind=HarnessKind.CODEX, seed="scope-one", auth_file=path)
    _, virtual = vault.snapshot()
    assert "real-" not in str(virtual)
    assert virtual is not None
    token = virtual["tokens"]["access_token"]
    assert vault.authentication_header("Bearer " + token, "openai") == "Bearer real-access"
    with pytest.raises(UnsupportedCredential):
        vault.substitute(token, "anthropic")
    other = CredentialVault(kind=HarnessKind.CODEX, seed="scope-two", auth_file=path)
    other.snapshot()
    with pytest.raises(UnsupportedCredential):
        other.substitute(token, "openai")
    assert (
        vault.refresh_request({"refresh_token": virtual["tokens"]["refresh_token"]}, "openai")[
            "refresh_token"
        ]
        == "real-refresh"
    )
    response = vault.refreshed(
        {"access_token": "rotated-access", "refresh_token": "rotated-refresh"}, "openai"
    )
    assert "rotated" not in str(response)
    assert "rotated-access" in path.read_text()
    vault.snapshot()
    assert vault.substitute(token, "openai") == "rotated-access"
    assert path.stat().st_mode & 0o777 == 0o600


def test_unknown_secret_format_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    atomic_json(path, {"custom_password": "never-copy"})
    with pytest.raises(UnsupportedCredential):
        CredentialVault(kind=HarnessKind.CLAUDE, seed="scope", auth_file=path).snapshot()


def test_muse_api_key_uses_fixed_private_gateway_and_rejects_custom_origins(tmp_path: Path) -> None:
    vault = CredentialVault(
        kind=HarnessKind.MUSE, seed="scope", api_keys={"META_API_KEY": "real-key"}
    )
    env, document = vault.snapshot()
    assert document is not None
    assert document["providers"]["meta"]["api_base_url"] == META_GATEWAY_BASE
    assert document["providers"]["meta"]["api_key"] == env["META_API_KEY"]
    assert vault.substitute(env["META_API_KEY"], "meta") == "real-key"
    path = tmp_path / "muse.json"
    for document in (
        {"providers": {"meta": {"api_key": "real-key", "api_base_url": "https://evil.example"}}},
        {"api_key": "real-key"},
        ["real-key"],
    ):
        atomic_json(path, document)
        with pytest.raises(UnsupportedCredential):
            CredentialVault(kind=HarnessKind.MUSE, seed="scope", auth_file=path).snapshot()
    atomic_json(path, {"providers": {"meta": {"api_key": "real-key"}}})
    _, document = CredentialVault(kind=HarnessKind.MUSE, seed="scope", auth_file=path).snapshot()
    assert document is not None
    assert document["providers"]["meta"]["api_base_url"] == META_GATEWAY_BASE


def test_jwt_claims_survive_but_signature_and_basic_credentials_are_handles(tmp_path: Path) -> None:
    claims = base64.urlsafe_b64encode(b'{"sub":"account","exp":2000000000}').decode().rstrip("=")
    real = "eyJhbGciOiJSUzI1NiJ9." + claims + ".provider-signature"
    path = tmp_path / "auth.json"
    atomic_json(path, {"tokens": {"access_token": real}})
    vault = CredentialVault(kind=HarnessKind.CODEX, seed="scope", auth_file=path)
    _, virtual = vault.snapshot()
    assert virtual is not None
    handle = virtual["tokens"]["access_token"]
    assert handle.split(".")[:2] == real.split(".")[:2]
    assert "provider-signature" not in handle
    encoded = base64.b64encode(("user:" + handle).encode()).decode()
    header = vault.authentication_header("Basic " + encoded, "openai")
    assert base64.b64decode(header[6:]).decode() == "user:" + real
    newer_claims = (
        base64.urlsafe_b64encode(b'{"sub":"account","exp":2100000000}').decode().rstrip("=")
    )
    rotated = "eyJhbGciOiJSUzI1NiJ9." + newer_claims + ".rotated-signature"
    atomic_json(path, {"tokens": {"access_token": rotated}})
    # A restarted gateway can still authenticate an earlier handle, without
    # accepting edits to its claims or exposing either provider signature.
    vault = CredentialVault(kind=HarnessKind.CODEX, seed="scope", auth_file=path)
    vault.snapshot()
    assert vault.substitute(handle, "openai") == rotated
    tampered = handle.replace(claims, newer_claims)
    with pytest.raises(UnsupportedCredential):
        vault.substitute(tampered, "openai")
    with pytest.raises(UnsupportedCredential):
        vault.authentication_header("Basic " + base64.b64encode(b"no-colon").decode(), "openai")


def test_multi_provider_oauth_refresh_updates_native_fields_and_expiry(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    atomic_json(
        path,
        {
            "openai": {
                "type": "oauth",
                "access": "old",
                "refresh": "old-refresh",
                "expires": 1_700_000_000_000,
            },
            "anthropic": {"type": "api", "key": "other-provider"},
        },
    )
    vault = CredentialVault(kind=HarnessKind.OPENCODE, seed="scope", auth_file=path)
    _, virtual = vault.snapshot()
    assert virtual is not None
    with pytest.raises(UnsupportedCredential):
        vault.substitute(virtual["anthropic"]["key"], "openai")
    response = vault.refreshed(
        {"access_token": "new", "refresh_token": "new-refresh", "expires_in": 3600}, "openai"
    )
    assert vault.substitute(response["access_token"], "openai") == "new"
    native = json.loads(path.read_text())
    assert native["openai"]["access"] == "new"
    assert native["openai"]["refresh"] == "new-refresh"
    assert native["openai"]["expires"] > time.time() * 1000
    assert native["anthropic"]["key"] == "other-provider"


@pytest.mark.django_db(transaction=True)
async def test_policy_owner_revision_and_conflict(tmp_path: Path) -> None:
    store = DjangoSandboxPolicyStore()
    policy_id = uuid4()
    first = await store.save(
        "owner",
        policy_id,
        SaveSandboxPolicy(policy=SandboxPolicy(project_root=str(tmp_path)), expected_revision=0),
    )
    second = await store.save(
        "owner",
        policy_id,
        SaveSandboxPolicy(
            policy=first.policy.model_copy(update={"egress": ()}), expected_revision=1
        ),
    )
    assert second.ref.revision == 2 and not second.policy.egress
    assert (await store.get("owner", policy_id, 1)).policy.egress == first.policy.egress
    with pytest.raises(DomainError) as conflict:
        await store.save(
            "owner", policy_id, SaveSandboxPolicy(policy=first.policy, expected_revision=1)
        )
    assert conflict.value.code is ErrorCode.OPTIMISTIC_CONFLICT
    with pytest.raises(DomainError) as denied:
        await store.get("other-owner", policy_id)
    assert denied.value.code is ErrorCode.NOT_FOUND
