"""Table tests for pure approval rule matching (deny-wins)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from talktoharnesses.domain import (
    ApprovalMatcher,
    ApprovalRule,
    ApprovalRuleDecision,
    ApprovalRuleScope,
    BlanketNetworkMatcher,
    CommandApprovalAction,
    ConversationHarnessBinding,
    ConversationRuleScope,
    ExactArgvMatcher,
    ExactPathMatcher,
    ExecutableRuleScope,
    FileApprovalAction,
    FileOperation,
    HarnessConfiguration,
    HarnessInstanceRuleScope,
    HarnessKind,
    InteractionMatchContext,
    LaunchSnapshot,
    NetworkApprovalAction,
    PrincipalGlobalRuleScope,
    RecursiveDirectoryMatcher,
    UserRuleScope,
    normalize_approval_action,
    normalize_approval_rule,
    path_is_under_directory,
    select_matching_rule,
)
from talktoharnesses.domain.models import HarnessCapabilities


def _now() -> datetime:
    return datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


def test_normalize_path_directory_and_recursive_matcher_edges(tmp_path: Path) -> None:
    from talktoharnesses.domain.approval_matching import (
        normalize_approval_path,
        normalize_directory,
    )

    existing = tmp_path / "file.txt"
    existing.write_text("x")
    assert normalize_approval_path(str(existing), working_directory=None) == str(existing.resolve())
    missing_child = tmp_path / "new-file.txt"
    assert normalize_approval_path(
        str(missing_child),
        working_directory=None,
        allow_missing_final=True,
    ).endswith("new-file.txt")
    with pytest.raises(ValueError):
        normalize_approval_path(str(tmp_path / "nope" / "x"), allow_missing_final=True)
    with pytest.raises(ValueError):
        normalize_approval_path(str(tmp_path / "missing"), allow_missing_final=False)

    assert normalize_directory(str(tmp_path)) == str(tmp_path.resolve())
    with pytest.raises(ValueError):
        normalize_directory(str(tmp_path / "missing-dir"))
    with pytest.raises(ValueError):
        normalize_directory(str(existing))

    nested = tmp_path / "rel"
    nested.mkdir()
    rule = ApprovalRule(
        principal_id="p1",
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=RecursiveDirectoryMatcher(directory="rel", operation=FileOperation.READ),
        created_at=_now(),
        updated_at=_now(),
    )
    with pytest.raises(ValueError):
        normalize_approval_rule(rule)
    normalized = normalize_approval_rule(rule, working_directory=str(tmp_path))
    assert Path(normalized.matcher.directory).is_absolute()  # type: ignore[union-attr]
    assert Path(normalized.matcher.directory) == nested.resolve()  # type: ignore[union-attr]


def _ctx(
    *,
    principal_id: str = "p1",
    owner_id: str = "p1",
    conversation_id: UUID | None = None,
    harness_instance_id: UUID | None = None,
    resolved_executable: str | None = "/usr/bin/grok",
) -> InteractionMatchContext:
    cid = conversation_id or uuid4()
    hid = harness_instance_id or uuid4()
    caps = HarnessCapabilities(kind=HarnessKind.GROK, version="1.0.0")
    binding = ConversationHarnessBinding(
        conversation_id=cid,
        kind=HarnessKind.GROK,
        configuration=HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp/ws"),
        harness_instance_id=hid,
        launch_snapshot=LaunchSnapshot(
            resolved_executable=resolved_executable,
            harness_version="1.0.0",
            working_directory="/tmp/ws",
            adapter_version="0",
            capabilities=caps,
        ),
        created_at=_now(),
    )
    return InteractionMatchContext(
        principal_id=principal_id,
        conversation_id=cid,
        owner_id=owner_id,
        binding=binding,
        working_directory="/tmp/ws",
    )


def _rule(
    *,
    decision: ApprovalRuleDecision,
    scope: ApprovalRuleScope,
    matcher: ApprovalMatcher,
    principal_id: str = "p1",
    rule_id: UUID | None = None,
) -> ApprovalRule:
    return ApprovalRule(
        id=rule_id or uuid4(),
        principal_id=principal_id,
        decision=decision,
        scope=scope,
        matcher=matcher,
        created_at=_now(),
        updated_at=_now(),
    )


def test_exact_argv_distinguishes_boundaries() -> None:
    ctx = _ctx()
    allow = _rule(
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactArgvMatcher(argv=("tool", "a", "b")),
    )
    action = CommandApprovalAction(argv=("tool", "a b"))
    assert select_matching_rule([allow], action=action, ctx=ctx).decision is None
    action2 = CommandApprovalAction(argv=("tool", "a", "b"))
    assert (
        select_matching_rule([allow], action=action2, ctx=ctx).decision
        is ApprovalRuleDecision.ALLOW
    )


def test_argv_order_and_empty_matter() -> None:
    ctx = _ctx()
    allow = _rule(
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactArgvMatcher(argv=("a", "b")),
    )
    assert (
        select_matching_rule(
            [allow], action=CommandApprovalAction(argv=("b", "a")), ctx=ctx
        ).decision
        is None
    )
    allow_empty = _rule(
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactArgvMatcher(argv=("tool", "")),
    )
    assert (
        select_matching_rule(
            [allow_empty], action=CommandApprovalAction(argv=("tool", "")), ctx=ctx
        ).decision
        is ApprovalRuleDecision.ALLOW
    )


def test_path_component_containment_not_prefix(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    sibling = tmp_path / "proj-other"
    sibling.mkdir()
    child = root / "src" / "a.py"
    child.parent.mkdir()
    child.write_text("x")
    assert path_is_under_directory(str(child.resolve()), str(root.resolve()))
    assert not path_is_under_directory(str(sibling.resolve()), str(root.resolve()))


def test_exact_path_and_recursive_and_operation(tmp_path: Path) -> None:
    ctx = _ctx()
    f = tmp_path / "file.txt"
    f.write_text("x")
    resolved = str(f.resolve())
    exact = _rule(
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactPathMatcher(path=resolved, operation=FileOperation.READ),
    )
    action = FileApprovalAction(path=resolved, operation=FileOperation.READ)
    decision = select_matching_rule([exact], action=action, ctx=ctx).decision
    assert decision is ApprovalRuleDecision.ALLOW
    wrong_op = FileApprovalAction(path=resolved, operation=FileOperation.MODIFY)
    assert select_matching_rule([exact], action=wrong_op, ctx=ctx).decision is None

    d = tmp_path / "dir"
    d.mkdir()
    nested = d / "n.txt"
    nested.write_text("y")
    recursive = _rule(
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=RecursiveDirectoryMatcher(directory=str(d.resolve()), operation=FileOperation.READ),
    )
    nested_action = FileApprovalAction(path=str(nested.resolve()), operation=FileOperation.READ)
    assert (
        select_matching_rule([recursive], action=nested_action, ctx=ctx).decision
        is ApprovalRuleDecision.ALLOW
    )


def test_file_action_and_rule_match_after_canonicalization(tmp_path: Path) -> None:
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    target = workdir / "new.txt"
    action = normalize_approval_action(
        FileApprovalAction(path="new.txt", operation=FileOperation.CREATE),
        working_directory=str(workdir),
    )
    rule = normalize_approval_rule(
        _rule(
            decision=ApprovalRuleDecision.ALLOW,
            scope=PrincipalGlobalRuleScope(),
            matcher=ExactPathMatcher(path="new.txt", operation=FileOperation.CREATE),
        ),
        working_directory=str(workdir),
    )

    assert isinstance(action, FileApprovalAction)
    assert action.path == str(target)
    assert rule.matcher.path == str(target)  # type: ignore[union-attr]


def test_standalone_relative_path_rule_is_rejected() -> None:
    rule = _rule(
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactPathMatcher(path="same-name", operation=FileOperation.CREATE),
    )

    with pytest.raises(ValueError):
        normalize_approval_rule(rule)


def test_blanket_network_only_matches_network() -> None:
    ctx = _ctx()
    net = _rule(
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=BlanketNetworkMatcher(),
    )
    assert (
        select_matching_rule([net], action=NetworkApprovalAction(), ctx=ctx).decision
        is ApprovalRuleDecision.ALLOW
    )
    assert (
        select_matching_rule(
            [net], action=CommandApprovalAction(argv=("curl", "https://x")), ctx=ctx
        ).decision
        is None
    )


def test_deny_wins_over_more_specific_allow() -> None:
    ctx = _ctx()
    conv_allow = _rule(
        decision=ApprovalRuleDecision.ALLOW,
        scope=ConversationRuleScope(conversation_id=ctx.conversation_id),
        matcher=ExactArgvMatcher(argv=("rm", "-rf", "/")),
    )
    global_deny = _rule(
        decision=ApprovalRuleDecision.DENY,
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactArgvMatcher(argv=("rm", "-rf", "/")),
    )
    action = CommandApprovalAction(argv=("rm", "-rf", "/"))
    result = select_matching_rule([conv_allow, global_deny], action=action, ctx=ctx)
    assert result.decision is ApprovalRuleDecision.DENY
    assert result.rule is not None
    assert result.rule.id == global_deny.id


def test_specificity_prefers_conversation_scope_for_audit() -> None:
    ctx = _ctx()
    conv_deny = _rule(
        decision=ApprovalRuleDecision.DENY,
        scope=ConversationRuleScope(conversation_id=ctx.conversation_id),
        matcher=ExactArgvMatcher(argv=("ls",)),
    )
    global_deny = _rule(
        decision=ApprovalRuleDecision.DENY,
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactArgvMatcher(argv=("ls",)),
    )
    action = CommandApprovalAction(argv=("ls",))
    result = select_matching_rule([global_deny, conv_deny], action=action, ctx=ctx)
    assert result.rule is not None
    assert result.rule.id == conv_deny.id


def test_scope_boundaries() -> None:
    ctx = _ctx()
    other_conv = _rule(
        decision=ApprovalRuleDecision.ALLOW,
        scope=ConversationRuleScope(conversation_id=uuid4()),
        matcher=ExactArgvMatcher(argv=("ls",)),
    )
    harness = _rule(
        decision=ApprovalRuleDecision.ALLOW,
        scope=HarnessInstanceRuleScope(
            harness_instance_id=ctx.binding.harness_instance_id  # type: ignore[union-attr]
        ),
        matcher=ExactArgvMatcher(argv=("ls",)),
    )
    exe = _rule(
        decision=ApprovalRuleDecision.ALLOW,
        scope=ExecutableRuleScope(resolved_executable="/usr/bin/grok"),
        matcher=ExactArgvMatcher(argv=("ls",)),
    )
    user = _rule(
        decision=ApprovalRuleDecision.ALLOW,
        scope=UserRuleScope(user_id="p1"),
        matcher=ExactArgvMatcher(argv=("ls",)),
    )
    action = CommandApprovalAction(argv=("ls",))
    assert select_matching_rule([other_conv], action=action, ctx=ctx).decision is None
    assert (
        select_matching_rule([harness], action=action, ctx=ctx).decision
        is ApprovalRuleDecision.ALLOW
    )
    assert (
        select_matching_rule([exe], action=action, ctx=ctx).decision is ApprovalRuleDecision.ALLOW
    )
    assert (
        select_matching_rule([user], action=action, ctx=ctx).decision is ApprovalRuleDecision.ALLOW
    )


def test_manual_only_when_action_missing() -> None:
    ctx = _ctx()
    allow = _rule(
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactArgvMatcher(argv=("ls",)),
    )
    assert select_matching_rule([allow], action=None, ctx=ctx).decision is None


def test_principal_isolation() -> None:
    ctx = _ctx(principal_id="p1")
    foreign = _rule(
        decision=ApprovalRuleDecision.ALLOW,
        principal_id="p2",
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactArgvMatcher(argv=("ls",)),
    )
    assert (
        select_matching_rule(
            [foreign], action=CommandApprovalAction(argv=("ls",)), ctx=ctx
        ).decision
        is None
    )


@pytest.mark.parametrize("op", list(FileOperation))
def test_each_file_operation_matches_exactly(op: FileOperation, tmp_path: Path) -> None:
    ctx = _ctx()
    f = tmp_path / f"{op.value}.txt"
    f.write_text("x")
    path = str(f.resolve())
    rule = _rule(
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactPathMatcher(path=path, operation=op),
    )
    action = FileApprovalAction(path=path, operation=op)
    match = select_matching_rule([rule], action=action, ctx=ctx).decision
    assert match is ApprovalRuleDecision.ALLOW
    for other in FileOperation:
        if other is op:
            continue
        wrong = FileApprovalAction(path=path, operation=other)
        assert select_matching_rule([rule], action=wrong, ctx=ctx).decision is None


def test_matcher_and_scope_unions_reject_unknown() -> None:
    from pydantic import TypeAdapter, ValidationError

    from talktoharnesses.domain.models import ApprovalMatcher, ApprovalRuleScope

    with pytest.raises(ValidationError):
        TypeAdapter(ApprovalMatcher).validate_python({"kind": "glob", "pattern": "*"})
    with pytest.raises(ValidationError):
        TypeAdapter(ApprovalRuleScope).validate_python({"kind": "team", "team_id": "x"})
    with pytest.raises(ValidationError):
        TypeAdapter(ApprovalMatcher).validate_python(
            {"kind": "exact_argv", "argv": ["a"], "extra": True}
        )


def test_exact_path_more_specific_than_recursive(tmp_path: Path) -> None:
    ctx = _ctx()
    d = tmp_path / "d"
    d.mkdir()
    f = d / "f.txt"
    f.write_text("x")
    resolved = str(f.resolve())
    exact_allow = _rule(
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactPathMatcher(path=resolved, operation=FileOperation.READ),
        rule_id=uuid4(),
    )
    recursive_deny = _rule(
        decision=ApprovalRuleDecision.DENY,
        scope=PrincipalGlobalRuleScope(),
        matcher=RecursiveDirectoryMatcher(directory=str(d.resolve()), operation=FileOperation.READ),
        rule_id=uuid4(),
    )
    action = FileApprovalAction(path=resolved, operation=FileOperation.READ)
    # Deny still wins even if less specific matcher.
    result = select_matching_rule([exact_allow, recursive_deny], action=action, ctx=ctx)
    assert result.decision is ApprovalRuleDecision.DENY
