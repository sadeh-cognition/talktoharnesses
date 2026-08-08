"""Pure approval rule normalization and selection (deny-wins).

Used by the interaction broker, persistence transactions, and tests.
Rule evaluation never mutates a rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import UUID

from talktoharnesses.domain.enums import ApprovalRuleDecision, ApprovalRuleScopeKind, FileOperation
from talktoharnesses.domain.models import (
    ApprovalAction,
    ApprovalMatcher,
    ApprovalRule,
    ApprovalRuleScope,
    CommandApprovalAction,
    ConversationHarnessBinding,
    ConversationRuleScope,
    ExactArgvMatcher,
    ExactPathMatcher,
    ExecutableRuleScope,
    FileApprovalAction,
    HarnessInstanceRuleScope,
    NetworkApprovalAction,
    RecursiveDirectoryMatcher,
    UserRuleScope,
)

_SCOPE_RANK: dict[str, int] = {
    ApprovalRuleScopeKind.CONVERSATION.value: 0,
    ApprovalRuleScopeKind.HARNESS_INSTANCE.value: 1,
    ApprovalRuleScopeKind.EXECUTABLE.value: 2,
    ApprovalRuleScopeKind.USER.value: 3,
    ApprovalRuleScopeKind.PRINCIPAL_GLOBAL.value: 4,
}

_MATCHER_RANK: dict[str, int] = {
    "exact_argv": 0,
    "exact_path": 0,
    "recursive_directory": 1,
    "blanket_network": 0,
}


@dataclass(frozen=True, slots=True)
class InteractionMatchContext:
    """Context used to decide whether a rule's scope applies."""

    principal_id: str
    conversation_id: UUID
    owner_id: str
    binding: ConversationHarnessBinding | None
    working_directory: str | None = None


@dataclass(frozen=True, slots=True)
class MatchDecision:
    """Result of automatic rule evaluation for one interaction."""

    decision: ApprovalRuleDecision | None
    rule: ApprovalRule | None


def normalize_approval_path(
    path: str,
    *,
    working_directory: str | None = None,
    allow_missing_final: bool = False,
) -> str:
    """Resolve a path for rule/request matching.

    Existing path components are symlink-resolved. When ``allow_missing_final``
    is true (create targets), the final component may not exist.
    """
    raw = Path(path)
    if not raw.is_absolute():
        if not working_directory:
            msg = "relative path requires working_directory"
            raise ValueError(msg)
        raw = Path(working_directory) / raw

    if allow_missing_final:
        parent = raw.parent
        try:
            resolved_parent = parent.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            msg = f"path parent not found: {path}"
            raise ValueError(msg) from exc
        return str(resolved_parent / raw.name)

    try:
        return str(raw.resolve(strict=True))
    except (FileNotFoundError, OSError) as exc:
        msg = f"path not found: {path}"
        raise ValueError(msg) from exc


def normalize_directory(path: str) -> str:
    """Resolve an existing directory for recursive matchers."""
    try:
        resolved = Path(path).resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        msg = f"directory not found: {path}"
        raise ValueError(msg) from exc
    if not resolved.is_dir():
        msg = f"path is not a directory: {path}"
        raise ValueError(msg)
    return str(resolved)


def normalize_approval_action(
    action: ApprovalAction | None,
    *,
    working_directory: str | None,
) -> ApprovalAction | None:
    """Canonicalize a provider action before it is persisted or matched."""
    if not isinstance(action, FileApprovalAction):
        return action
    return action.model_copy(
        update={
            "path": normalize_approval_path(
                action.path,
                working_directory=working_directory,
                allow_missing_final=action.operation is FileOperation.CREATE,
            )
        }
    )


def normalize_approval_rule(
    rule: ApprovalRule,
    *,
    working_directory: str | None = None,
) -> ApprovalRule:
    """Canonicalize executable scopes and path matchers before persistence."""
    scope = rule.scope
    if isinstance(scope, ExecutableRuleScope):
        from talktoharnesses.runtime.paths import resolve_executable

        scope = scope.model_copy(
            update={"resolved_executable": str(resolve_executable(scope.resolved_executable))}
        )
    matcher = rule.matcher
    if isinstance(matcher, ExactPathMatcher):
        if not Path(matcher.path).is_absolute() and working_directory is None:
            raise ValueError("exact path matcher requires an absolute path")
        matcher = matcher.model_copy(
            update={
                "path": normalize_approval_path(
                    matcher.path,
                    working_directory=working_directory,
                    allow_missing_final=matcher.operation is FileOperation.CREATE,
                )
            }
        )
    elif isinstance(matcher, RecursiveDirectoryMatcher):
        raw = Path(matcher.directory)
        if not raw.is_absolute():
            if working_directory is None:
                raise ValueError("recursive directory matcher requires an absolute path")
            raw = Path(working_directory) / raw
        matcher = matcher.model_copy(update={"directory": normalize_directory(str(raw))})
    return rule.model_copy(update={"scope": scope, "matcher": matcher})


def path_is_under_directory(path: str, directory: str) -> bool:
    """True when path is directory or a descendant (component-wise, not prefix)."""
    pure_path = _pure(path)
    pure_dir = _pure(directory)
    if pure_path == pure_dir:
        return True
    try:
        pure_path.relative_to(pure_dir)
    except ValueError:
        return False
    return True


def _pure(path: str) -> PurePosixPath | PureWindowsPath:
    # Prefer platform-native pure path so drive letters / separators compare correctly.
    if len(path) >= 2 and path[1] == ":":
        return PureWindowsPath(path)
    return PurePosixPath(path) if path.startswith("/") else PureWindowsPath(path)


def scope_applies(scope: ApprovalRuleScope, ctx: InteractionMatchContext) -> bool:
    if isinstance(scope, ConversationRuleScope):
        return scope.conversation_id == ctx.conversation_id
    if isinstance(scope, HarnessInstanceRuleScope):
        if ctx.binding is None or ctx.binding.harness_instance_id is None:
            return False
        return scope.harness_instance_id == ctx.binding.harness_instance_id
    if isinstance(scope, ExecutableRuleScope):
        if ctx.binding is None or ctx.binding.launch_snapshot is None:
            return False
        return scope.resolved_executable == ctx.binding.launch_snapshot.resolved_executable
    if isinstance(scope, UserRuleScope):
        return scope.user_id == ctx.owner_id
    return True


def matcher_matches(matcher: ApprovalMatcher, action: ApprovalAction | None) -> bool:
    if action is None:
        return False
    if isinstance(matcher, ExactArgvMatcher):
        return isinstance(action, CommandApprovalAction) and matcher.argv == action.argv
    if isinstance(matcher, ExactPathMatcher):
        return (
            isinstance(action, FileApprovalAction)
            and matcher.path == action.path
            and matcher.operation is action.operation
        )
    if isinstance(matcher, RecursiveDirectoryMatcher):
        return (
            isinstance(action, FileApprovalAction)
            and matcher.operation is action.operation
            and path_is_under_directory(action.path, matcher.directory)
        )
    return isinstance(action, NetworkApprovalAction)


def _specificity_key(rule: ApprovalRule) -> tuple[int, int, str]:
    scope_rank = _SCOPE_RANK.get(rule.scope.kind, 99)
    matcher_rank = _MATCHER_RANK.get(rule.matcher.kind, 99)
    return (scope_rank, matcher_rank, str(rule.id))


def select_matching_rule(
    rules: list[ApprovalRule],
    *,
    action: ApprovalAction | None,
    ctx: InteractionMatchContext,
) -> MatchDecision:
    """Collect all applicable matching rules; deny wins; else most specific allow.

    Specificity order is used only to choose which rule is recorded for audit.
    """
    matching: list[ApprovalRule] = []
    for rule in rules:
        if rule.principal_id != ctx.principal_id:
            continue
        if not scope_applies(rule.scope, ctx):
            continue
        if not matcher_matches(rule.matcher, action):
            continue
        matching.append(rule)

    if not matching:
        return MatchDecision(decision=None, rule=None)

    denies = [r for r in matching if r.decision is ApprovalRuleDecision.DENY]
    if denies:
        winner = min(denies, key=_specificity_key)
        return MatchDecision(decision=ApprovalRuleDecision.DENY, rule=winner)

    allows = [r for r in matching if r.decision is ApprovalRuleDecision.ALLOW]
    if allows:
        winner = min(allows, key=_specificity_key)
        return MatchDecision(decision=ApprovalRuleDecision.ALLOW, rule=winner)

    return MatchDecision(decision=None, rule=None)


def rule_matches_request(
    rule: ApprovalRule,
    *,
    action: ApprovalAction | None,
    ctx: InteractionMatchContext,
) -> bool:
    """Whether a proposed create-and-allow rule matches the current request context."""
    if rule.principal_id != ctx.principal_id:
        return False
    if not scope_applies(rule.scope, ctx):
        return False
    return matcher_matches(rule.matcher, action)


def file_operation_from_action(action: ApprovalAction | None) -> FileOperation | None:
    if isinstance(action, FileApprovalAction):
        return action.operation
    return None
