"""Codex app-server JSON-RPC method name constants.

Names match ``openai/codex`` ``codex-rs/app-server-protocol`` (and T3's
generated client). Regenerate alongside models via
``scripts/generate_codex_models.py`` when upstream drifts.
"""

from __future__ import annotations


class ClientMethods:
    """Client → server requests."""

    INITIALIZE = "initialize"
    THREAD_START = "thread/start"
    THREAD_RESUME = "thread/resume"
    TURN_START = "turn/start"
    TURN_INTERRUPT = "turn/interrupt"


class ClientNotifications:
    """Client → server notifications."""

    INITIALIZED = "initialized"


class Notifications:
    """Server → client notifications we normalize."""

    THREAD_STARTED = "thread/started"
    THREAD_TOKEN_USAGE_UPDATED = "thread/tokenUsage/updated"
    THREAD_STATUS_CHANGED = "thread/status/changed"
    TURN_STARTED = "turn/started"
    TURN_COMPLETED = "turn/completed"
    TURN_PLAN_UPDATED = "turn/plan/updated"
    TURN_DIFF_UPDATED = "turn/diff/updated"
    ITEM_STARTED = "item/started"
    ITEM_COMPLETED = "item/completed"
    ITEM_AGENT_MESSAGE_DELTA = "item/agentMessage/delta"
    ITEM_REASONING_TEXT_DELTA = "item/reasoning/textDelta"
    ITEM_REASONING_SUMMARY_TEXT_DELTA = "item/reasoning/summaryTextDelta"
    ITEM_COMMAND_EXECUTION_OUTPUT_DELTA = "item/commandExecution/outputDelta"
    ITEM_FILE_CHANGE_OUTPUT_DELTA = "item/fileChange/outputDelta"
    ITEM_PLAN_DELTA = "item/plan/delta"


class ServerRequests:
    """Server → client requests (approvals / user input)."""

    COMMAND_EXECUTION_REQUEST_APPROVAL = "item/commandExecution/requestApproval"
    FILE_CHANGE_REQUEST_APPROVAL = "item/fileChange/requestApproval"
    PERMISSIONS_REQUEST_APPROVAL = "item/permissions/requestApproval"
    TOOL_REQUEST_USER_INPUT = "item/tool/requestUserInput"


# Convenience re-export used by the generation script's emission target.
ALL_CLIENT_METHODS = tuple(
    v for k, v in vars(ClientMethods).items() if not k.startswith("_")
)
ALL_NOTIFICATIONS = tuple(
    v for k, v in vars(Notifications).items() if not k.startswith("_")
)
ALL_SERVER_REQUESTS = tuple(
    v for k, v in vars(ServerRequests).items() if not k.startswith("_")
)
