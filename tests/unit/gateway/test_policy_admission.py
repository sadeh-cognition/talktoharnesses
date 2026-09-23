from pathlib import Path
from uuid import uuid4

import pytest
from tests.phase8_fixtures import NOW, idle_state
from tth_types.sandbox import CommandRule, SandboxPolicy, SaveSandboxPolicy

from talktoharnesses.application.broker import InProcessCommittedEventBroker
from talktoharnesses.application.service import TalkToHarnessesService
from talktoharnesses.django.persistence import DjangoPersistence
from talktoharnesses.django.sandbox_policies import DjangoSandboxPolicyStore
from talktoharnesses.domain import (
    ApprovalDecision,
    ApprovalRule,
    ApprovalRuleDecision,
    CommandApprovalAction,
    DomainError,
    ErrorCode,
    ExactArgvMatcher,
    HarnessConfiguration,
    HarnessKind,
    PrincipalGlobalRuleScope,
    request_interaction,
    start_turn,
    submit_turn,
)
from talktoharnesses.domain.enums import InteractionKind
from talktoharnesses.domain.models import (
    ApprovalRequestPayload,
    InteractionAnswer,
    PendingInteraction,
)
from talktoharnesses.providers.registry import AdapterRegistry
from talktoharnesses.runtime.manager import RuntimeManager


@pytest.mark.django_db(transaction=True)
async def test_new_conversations_take_latest_policy_existing_bindings_keep_revision(
    tmp_path: Path,
) -> None:
    persistence = DjangoPersistence()
    policies = DjangoSandboxPolicyStore()
    registry = AdapterRegistry()
    service = TalkToHarnessesService(
        persistence,
        registry,
        InProcessCommittedEventBroker(),
        lambda: NOW,
        RuntimeManager(persistence, registry, clock=lambda: NOW),
        sandbox_policies=policies,
    )
    configuration = HarnessConfiguration(kind=HarnessKind.CODEX, working_directory=str(tmp_path))
    with pytest.raises(DomainError) as missing:
        await service.create_harness("owner", name="missing", configuration=configuration)
    assert missing.value.code is ErrorCode.SANDBOX_POLICY_REQUIRED
    first = await service.save_sandbox_policy(
        "owner",
        uuid4(),
        SaveSandboxPolicy(policy=SandboxPolicy(project_root=str(tmp_path)), expected_revision=0),
    )
    configuration = configuration.model_copy(update={"sandbox_policy": first.ref})
    harness = await service.create_harness("owner", name="scoped", configuration=configuration)
    existing = await service.create_conversation("owner", harness.id)
    second = await service.save_sandbox_policy(
        "owner",
        first.ref.id,
        SaveSandboxPolicy(
            policy=first.policy.model_copy(update={"egress": ()}), expected_revision=1
        ),
    )
    fresh = await service.create_conversation("owner", harness.id)
    assert fresh.detail.sandbox_policy == second.ref
    resumed = await service.get_conversation("owner", existing.detail.conversation.id)
    assert resumed.detail.sandbox_policy == first.ref
    assert (await service.get_sandbox_policy("owner", first.ref.id)).ref == second.ref
    with pytest.raises(DomainError) as intruder:
        await service.create_harness("intruder", name="stolen", configuration=configuration)
    assert intruder.value.code is ErrorCode.NOT_FOUND


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("automatic", [False, True])
async def test_frozen_command_policy_overrides_manual_and_saved_allow(
    tmp_path: Path,
    automatic: bool,
) -> None:
    policies = DjangoSandboxPolicyStore()
    first = await policies.save(
        "owner",
        uuid4(),
        SaveSandboxPolicy(
            policy=SandboxPolicy(
                project_root=str(tmp_path), command_rules=(CommandRule(argv=("tool", "publish")),)
            ),
            expected_revision=0,
        ),
    )
    # Removing a rule from a later revision cannot relax an existing binding.
    await policies.save(
        "owner",
        first.ref.id,
        SaveSandboxPolicy(
            policy=first.policy.model_copy(update={"command_rules": ()}),
            expected_revision=1,
        ),
    )
    state = idle_state()
    assert state.binding is not None
    state = state.model_copy(
        update={
            "binding": state.binding.model_copy(
                update={
                    "configuration": state.binding.configuration.model_copy(
                        update={"sandbox_policy": first.ref}
                    )
                }
            )
        }
    )
    persistence = DjangoPersistence()
    await persistence.save_snapshot(state)
    await persistence.create_approval_rule(
        ApprovalRule(
            principal_id="owner",
            decision=ApprovalRuleDecision.ALLOW,
            scope=PrincipalGlobalRuleScope(),
            matcher=ExactArgvMatcher(argv=("tool", "publish")),
            created_at=NOW,
            updated_at=NOW,
        )
    )
    queued = submit_turn(state, prompt="publish", idempotency_key="turn", now=NOW)
    running = start_turn(queued.state, now=NOW)
    assert running.state.active_turn is not None
    interaction = PendingInteraction(
        conversation_id=state.conversation.id,
        turn_id=running.state.active_turn.id,
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(
            action=CommandApprovalAction(argv=("tool", "publish")),
            available_decisions=(ApprovalDecision.ALLOW_ONCE, ApprovalDecision.DENY),
        ),
        created_at=NOW,
    )
    requested = request_interaction(running.state, interaction, now=NOW)
    await persistence.commit_interaction_request(
        state.conversation.id,
        state.conversation.version,
        requested.state,
        (*queued.events, *running.events, *requested.events),
        interaction_id=interaction.id,
        provider_correlation={},
        request_event_sequence=requested.events[-1].sequence,
    )

    async def resolve():
        return await persistence.commit_interaction_resolution(
            state.conversation.id,
            "owner",
            requested.state.conversation.version,
            requested.state,
            (),
            InteractionAnswer(
                interaction_id=interaction.id,
                decision=None if automatic else ApprovalDecision.ALLOW_ONCE,
                submitted_at=NOW,
            ),
            automatic=automatic,
            resolution_event_sequence=0,
        )

    if automatic:
        result = await resolve()
        assert result.answer.decision is ApprovalDecision.DENY
        assert result.audit is not None and result.audit.deciding_rule_id is None
    else:
        with pytest.raises(DomainError) as denied:
            await resolve()
        assert denied.value.code is ErrorCode.SANDBOX_POLICY_DENIED


@pytest.mark.django_db(transaction=True)
async def test_invalid_mount_does_not_advance_policy_revision(tmp_path: Path) -> None:
    store = DjangoSandboxPolicyStore()
    policy_id = uuid4()
    with pytest.raises(DomainError) as denied:
        await store.save(
            "owner",
            policy_id,
            SaveSandboxPolicy(
                policy=SandboxPolicy(project_root=str(tmp_path / "missing")),
                expected_revision=0,
            ),
        )
    assert denied.value.code is ErrorCode.SANDBOX_POLICY_DENIED
    saved = await store.save(
        "owner",
        policy_id,
        SaveSandboxPolicy(
            policy=SandboxPolicy(project_root=str(tmp_path)),
            expected_revision=0,
        ),
    )
    assert saved.ref.revision == 1


@pytest.mark.django_db(transaction=True)
async def test_mcp_header_credentials_are_admitted_but_url_credentials_are_not(
    tmp_path: Path,
) -> None:
    from tth_types.harness import HarnessMcpHeader, HarnessMcpServer

    persistence = DjangoPersistence()
    registry = AdapterRegistry()
    service = TalkToHarnessesService(
        persistence,
        registry,
        InProcessCommittedEventBroker(),
        lambda: NOW,
        RuntimeManager(persistence, registry, clock=lambda: NOW),
        sandbox_policies=DjangoSandboxPolicyStore(),
    )
    policy = await service.save_sandbox_policy(
        "owner",
        uuid4(),
        SaveSandboxPolicy(policy=SandboxPolicy(project_root=str(tmp_path)), expected_revision=0),
    )
    configuration = HarnessConfiguration(
        kind=HarnessKind.CODEX,
        working_directory=str(tmp_path),
        sandbox_policy=policy.ref,
        mcp_servers=(
            HarnessMcpServer(
                name="memory",
                url="http://localhost:8001/mcp",
                headers=(HarnessMcpHeader(name="Authorization", value="Bearer secret"),),
            ),
        ),
    )
    harness = await service.create_harness("owner", name="mcp", configuration=configuration)
    assert harness.configuration.mcp_servers == configuration.mcp_servers
    for url in ("http://user:secret@localhost:8001/mcp", "http://localhost:8001/mcp?key=secret"):
        leaky = configuration.model_copy(
            update={"mcp_servers": (HarnessMcpServer(name="memory", url=url),)}
        )
        with pytest.raises(DomainError) as denied:
            await service.create_harness("owner", name="leaky", configuration=leaky)
        assert denied.value.code is ErrorCode.SANDBOX_POLICY_DENIED
