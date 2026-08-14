"""Reviewed public Python surface contract."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys

import pytest

APPROVED: dict[str, frozenset[str]] = {
    "talktoharnesses": frozenset({"__version__"}),
    "talktoharnesses.client": frozenset(
        {
            "APIError",
            "AsyncTalkToHarnessesClient",
            "ConversationStreamItem",
        }
    ),
    "talktoharnesses.application": frozenset(
        {
            "CommittedEventBroker",
            "CommittedEventPublisher",
            "ConversationWakeup",
            "Persistence",
            "StreamingTextRedactor",
            "TalkToHarnessesService",
        }
    ),
    "talktoharnesses.providers": frozenset(
        {
            "AdapterRegistry",
            "HarnessAdapter",
            "HarnessInteractionRequest",
            "HarnessSession",
            "ResumeSessionRequest",
            "StartSessionRequest",
            "SteerRequest",
            "TurnRequest",
            "build_default_adapter_registry",
        }
    ),
    "talktoharnesses.runtime": frozenset(
        {
            "STDERR_RETENTION_BYTES",
            "ManagedRuntime",
            "ProcessEvent",
            "ProcessExitedEvent",
            "ProcessForcedTerminationEvent",
            "ProcessHandle",
            "ProcessSilenceWarningEvent",
            "ProcessSpec",
            "ProcessStartedEvent",
            "ProcessStderrTruncatedEvent",
            "ProcessSupervisor",
            "RuntimeManager",
            "RuntimePolicy",
        }
    ),
    "talktoharnesses.django": frozenset({"DjangoPersistence"}),
    "talktoharnesses.providers.grok": frozenset({"GrokAdapter"}),
    "talktoharnesses.providers.cursor": frozenset({"CursorAdapter"}),
    "talktoharnesses.providers.codex": frozenset({"CodexAdapter"}),
    "talktoharnesses.providers.claude": frozenset({"ClaudeAdapter"}),
    "talktoharnesses.providers.opencode": frozenset({"OpenCodeAdapter"}),
    "talktoharnesses.providers.prime_agent": frozenset({"PrimeAgentAdapter"}),
    "talktoharnesses.providers.acp": frozenset(),
    "talktoharnesses.domain": frozenset(
        {
            "ActivityProjection",
            "ActivityStatus",
            "ApprovalAction",
            "ApprovalDecision",
            "ApprovalMatcher",
            "ApprovalRequestPayload",
            "ApprovalRule",
            "ApprovalRuleDecision",
            "ApprovalRuleInput",
            "ApprovalRuleProjection",
            "ApprovalRuleScope",
            "ApprovalRuleScopeKind",
            "BackgroundActivity",
            "BlanketNetworkMatcher",
            "CanonicalToolResult",
            "Command",
            "CommandApprovalAction",
            "CommandKind",
            "CommandProjection",
            "CommandStatus",
            "Conversation",
            "ConversationDetail",
            "ConversationEvent",
            "ConversationHarnessBinding",
            "ConversationRuleScope",
            "ConversationSearchHit",
            "ConversationShell",
            "ConversationSnapshot",
            "ConversationState",
            "ConversationStatus",
            "DomainError",
            "ErrorCode",
            "ErrorProjection",
            "EventPayload",
            "ExactArgvMatcher",
            "ExactPathMatcher",
            "ExecutableRuleScope",
            "ExpectedEvent",
            "FileApprovalAction",
            "FileChange",
            "FileOperation",
            "HarnessCapabilities",
            "HarnessConfiguration",
            "HarnessEffortInfo",
            "HarnessEvent",
            "HarnessInstance",
            "HarnessInstanceRuleScope",
            "HarnessKind",
            "HarnessModeInfo",
            "HarnessModelInfo",
            "HarnessProbeProjection",
            "HarnessProjection",
            "InteractionAnswer",
            "InteractionAuditProjection",
            "InteractionKind",
            "InteractionMatchContext",
            "InteractionProjection",
            "InteractionResolutionResult",
            "InteractionStatus",
            "LaunchSnapshot",
            "MatchDecision",
            "Message",
            "MessageChunk",
            "MessageProjection",
            "MessageRole",
            "NativeIORecord",
            "NetworkApprovalAction",
            "Page",
            "PendingInteraction",
            "Plan",
            "PlanItem",
            "PlanProjection",
            "PrincipalGlobalRuleScope",
            "ProcessRecord",
            "ProcessStatus",
            "ReadinessProjection",
            "ReasoningBlock",
            "RecursiveDirectoryMatcher",
            "RetentionPolicyProjection",
            "RetentionPreviewProjection",
            "SearchSnippet",
            "SubmitTurnResult",
            "SyncProjection",
            "TokenProjection",
            "ToolOutcome",
            "ToolOutputChunk",
            "ToolProjection",
            "TranscriptDocument",
            "TranscriptFixture",
            "TranscriptMessage",
            "TranscriptTool",
            "TranscriptTurn",
            "TransitionResult",
            "Turn",
            "TurnProjection",
            "TurnStatus",
            "UsageRecord",
            "UserRuleScope",
            "append_events",
            "apply_native_title",
            "apply_steer",
            "archive_conversation",
            "assert_redaction",
            "cancel_interaction",
            "cancel_open_interactions",
            "cancel_queued_prompt",
            "change_mode",
            "close_session",
            "commit_switch",
            "complete_activity",
            "complete_turn",
            "conversation_event_adapter",
            "dump_transcript_document",
            "dump_transcript_fixture",
            "edit_queued_prompt",
            "event_payload_adapter",
            "fail_running_activities",
            "fail_session",
            "fail_switch",
            "fail_turn",
            "fixture_to_dict",
            "interrupt_turn",
            "load_transcript_document",
            "load_transcript_fixture",
            "mark_outcome_unknown",
            "mark_requires_recreation",
            "matcher_matches",
            "new_conversation_state",
            "normalize_approval_action",
            "normalize_approval_path",
            "normalize_approval_rule",
            "normalize_directory",
            "path_is_under_directory",
            "pin_conversation",
            "reap_session",
            "register_activity",
            "remember_native_ids",
            "request_interaction",
            "resume_session",
            "rotate_session",
            "rule_matches_request",
            "scope_applies",
            "select_matching_rule",
            "set_retention_exemption",
            "snooze_conversation",
            "soft_delete_conversation",
            "start_session",
            "start_turn",
            "submit_interaction_answer",
            "submit_turn",
            "unarchive_conversation",
            "unpin_conversation",
            "unsnooze_conversation",
            "update_interaction_draft",
        }
    ),
}

IMPLEMENTATION_ONLY = {
    "talktoharnesses.providers.grok": frozenset(
        {
            "GrokCompatibilityDoc",
            "GrokReleaseRecord",
            "load_grok_compatibility",
            "match_release",
            "render_supported_harnesses_markdown",
        }
    ),
    "talktoharnesses.providers.cursor": frozenset(
        {
            "CursorCompatibilityDoc",
            "CursorReleaseRecord",
            "load_cursor_compatibility",
            "match_release",
        }
    ),
    "talktoharnesses.providers.codex": frozenset(
        {
            "CodexCompatibilityDoc",
            "CodexReleaseRecord",
            "load_codex_compatibility",
            "match_release",
        }
    ),
    "talktoharnesses.providers.claude": frozenset(
        {
            "ClaudeCompatibilityDoc",
            "ClaudeReleaseRecord",
            "load_claude_compatibility",
            "match_release",
        }
    ),
    "talktoharnesses.providers.opencode": frozenset(
        {
            "OpenCodeCompatibilityDoc",
            "OpenCodeReleaseRecord",
            "load_opencode_compatibility",
            "match_release",
        }
    ),
    "talktoharnesses.providers.prime_agent": frozenset(
        {
            "PrimeAgentCompatibilityDoc",
            "PrimeAgentReleaseRecord",
            "load_prime_agent_compatibility",
            "match_release",
        }
    ),
    "talktoharnesses.providers.acp": frozenset(
        {
            "AcpConnection",
            "Delivered",
            "JsonRpcError",
            "JsonRpcErrorResponse",
            "JsonRpcNotification",
            "JsonRpcRequest",
            "JsonRpcSuccessResponse",
        }
    ),
}


@pytest.mark.parametrize("module_name", sorted(APPROVED))
def test_approved_all_resolves(module_name: str) -> None:
    module = importlib.import_module(module_name)
    assert frozenset(module.__all__) == APPROVED[module_name]
    for name in module.__all__:
        assert hasattr(module, name), name
        assert getattr(module, name) is not None


@pytest.mark.parametrize("module_name", sorted(IMPLEMENTATION_ONLY))
def test_implementation_names_absent_from_all(module_name: str) -> None:
    module = importlib.import_module(module_name)
    exported = frozenset(module.__all__)
    leaked = IMPLEMENTATION_ONLY[module_name] & exported
    assert not leaked, sorted(leaked)


def test_core_public_imports_in_fresh_interpreter() -> None:
    code = """
import sys
import talktoharnesses
import talktoharnesses.domain
import talktoharnesses.application
import talktoharnesses.providers
import talktoharnesses.runtime

assert talktoharnesses.__all__ == ["__version__"]
assert "django" not in sys.modules
assert "TalkToHarnessesService" in talktoharnesses.application.__all__
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
