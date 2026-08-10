"""Wire-stable domain enumerations."""

from __future__ import annotations

from enum import StrEnum


class HarnessKind(StrEnum):
    GROK = "grok"
    CURSOR = "cursor"
    CODEX = "codex"
    CLAUDE = "claude"
    OPENCODE = "opencode"
    PRIME_AGENT = "prime_agent"


class ConversationStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    BACKGROUND_ACTIVE = "background_active"
    ARCHIVED = "archived"


class TurnStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class CommandStatus(StrEnum):
    ACCEPTED = "accepted"
    CLAIMED = "claimed"
    DELIVERY_STARTED = "delivery_started"
    DELIVERED = "delivered"
    SETTLED = "settled"
    COALESCED = "coalesced"
    OUTCOME_UNKNOWN = "outcome_unknown"


class CommandKind(StrEnum):
    SUBMIT_TURN = "submit_turn"
    STEER = "steer"
    EDIT_QUEUED = "edit_queued"
    CANCEL_QUEUED = "cancel_queued"
    INTERRUPT = "interrupt"
    ANSWER_INTERACTION = "answer_interaction"
    SWITCH_HARNESS = "switch_harness"


class InteractionKind(StrEnum):
    APPROVAL = "approval"
    STRUCTURED_QUESTION = "structured_question"


class InteractionStatus(StrEnum):
    PENDING = "pending"
    DRAFT = "draft"
    SUBMITTED = "submitted"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


class ApprovalDecision(StrEnum):
    """Immediate provider answer for one interaction request."""

    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"
    DENY = "deny"
    CANCEL = "cancel"


class ApprovalRuleDecision(StrEnum):
    """Persistent rule outcome (distinct from immediate ApprovalDecision)."""

    ALLOW = "allow"
    DENY = "deny"


class ApprovalRuleScopeKind(StrEnum):
    CONVERSATION = "conversation"
    HARNESS_INSTANCE = "harness_instance"
    EXECUTABLE = "executable"
    USER = "user"
    PRINCIPAL_GLOBAL = "principal_global"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ActivityStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ProcessStatus(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    FAILED = "failed"
    TERMINATED = "terminated"
    ORPHANED = "orphaned"


class RecoveryTrigger(StrEnum):
    STARTUP = "startup"
    TAKEOVER = "takeover"
    SHUTDOWN = "shutdown"
    LEGACY = "legacy"


class RecoveryAction(StrEnum):
    NO_ACTION = "no_action"
    RECLAIM = "reclaim"
    OUTCOME_UNKNOWN = "outcome_unknown"
    NATIVE_RESUME = "native_resume"
    HANDOFF_FALLBACK = "handoff_fallback"
    INVARIANT_FAILURE = "invariant_failure"


class RecoveryResultCode(StrEnum):
    SUCCESS = "success"
    ABANDONED = "abandoned"
    FAILED = "failed"
    NO_ACTION = "no_action"
    LEGACY_UNKNOWN = "legacy_unknown"


class RecoveryReasonCode(StrEnum):
    WORKER_LOST = "worker_lost"
    DELIVERY_AMBIGUOUS = "delivery_ambiguous"
    RESUME_UNSUPPORTED = "resume_unsupported"
    RESUME_REJECTED = "resume_rejected"
    PROVIDER_INCOMPATIBLE = "provider_incompatible"
    EXECUTABLE_CHANGED = "executable_changed"
    UNCHANGED_LAUNCH = "unchanged_launch"
    RECOVERY_FALLBACK = "recovery_fallback"
    INVARIANT_FAILURE = "invariant_failure"
    LEGACY_UNKNOWN = "legacy_unknown"
    NO_ACTION = "no_action"


class ObservedDeliveryPhase(StrEnum):
    ACCEPTED = "accepted"
    CLAIMED = "claimed"
    DELIVERY_STARTED = "delivery_started"
    DELIVERED = "delivered"
    SETTLED = "settled"
    COALESCED = "coalesced"
    OUTCOME_UNKNOWN = "outcome_unknown"
    NONE = "none"


class FileOperation(StrEnum):
    READ = "read"
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"


class ToolOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ErrorCode(StrEnum):
    PERSISTENCE_REQUIRED = "persistence_required"
    CONVERSATION_BUSY = "conversation_busy"
    MODE_CHANGE_WHILE_ACTIVE = "mode_change_while_active"
    UNSUPPORTED_NATIVE_EVENT = "unsupported_native_event"
    PROTOCOL_ERROR = "protocol_error"
    PROVIDER_INCOMPATIBLE = "provider_incompatible"
    WORKING_DIRECTORY_NOT_FOUND = "working_directory_not_found"
    WORKSPACE_ROOT_NOT_FOUND = "workspace_root_not_found"
    INVALID_EXECUTABLE = "invalid_executable"
    EXECUTABLE_OWNER_MISMATCH = "executable_owner_mismatch"
    RUNTIME_TIMEOUT = "runtime_timeout"
    INVALID_STATE = "invalid_state"
    INTERACTION_ALREADY_RESOLVED = "interaction_already_resolved"
    QUEUED_PROMPT_NOT_EDITABLE = "queued_prompt_not_editable"
    UNKNOWN_HARNESS_KIND = "unknown_harness_kind"
    DUPLICATE_REGISTRATION = "duplicate_registration"
    HARNESS_NOT_REGISTERED = "harness_not_registered"
    NO_ACTIVE_TURN = "no_active_turn"
    NO_QUEUED_PROMPT = "no_queued_prompt"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    OPTIMISTIC_CONFLICT = "optimistic_conflict"
    STALE_OWNER = "stale_owner"
    WORKER_LEASE_UNAVAILABLE = "worker_lease_unavailable"
    INVALID_CURSOR = "invalid_cursor"
    INVALID_SEARCH_QUERY = "invalid_search_query"
    NOT_FOUND = "not_found"
