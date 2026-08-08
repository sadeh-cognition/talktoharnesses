"""Wire-stable domain enumerations."""

from __future__ import annotations

from enum import StrEnum


class HarnessKind(StrEnum):
    GROK = "grok"
    CURSOR = "cursor"
    CODEX = "codex"
    CLAUDE = "claude"
    OPENCODE = "opencode"


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
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"
    DENY = "deny"
    CANCEL = "cancel"


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
