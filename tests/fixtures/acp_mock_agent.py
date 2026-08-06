#!/usr/bin/env python3
"""Minimal ACP agent peer for Cursor/Grok driver tests.

Speaks enough of ACP JSON-RPC over stdio for:
  initialize → session/new → session/prompt (with agent_message_chunk updates)
  optional session/request_permission reverse-request mid-prompt

Env:
  TALKTOHARNESSES_ACP_APPROVAL=1  — raise a permission request before completing
  TALKTOHARNESSES_ACP_DECISIONS   — path to write permission response bodies
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def write(msg: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    want_approval = os.environ.get("TALKTOHARNESSES_ACP_APPROVAL") == "1"
    decisions_path = os.environ.get("TALKTOHARNESSES_ACP_DECISIONS")
    decisions: list[Any] = []
    session_id = "acp-session-1"

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue

        # Response to our reverse-request
        if "method" not in msg and "id" in msg:
            if decisions_path is not None:
                decisions.append(msg)
                Path(decisions_path).write_text(
                    json.dumps(decisions, indent=2), encoding="utf-8"
                )
            continue

        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": 1,
                        "agentCapabilities": {
                            "loadSession": True,
                        },
                        "agentInfo": {"name": "acp-mock", "version": "0.1"},
                        "authMethods": [
                            {"id": "cursor_login", "name": "Cursor Login"},
                            {"id": "xai.api_key", "name": "xAI API Key"},
                            {"id": "cached_token", "name": "Cached Token"},
                        ],
                    },
                }
            )
            continue

        if method == "authenticate":
            write({"jsonrpc": "2.0", "id": req_id, "result": {}})
            continue

        if method == "session/new":
            write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"sessionId": session_id},
                }
            )
            continue

        if method == "session/load":
            sid = params.get("sessionId") or session_id
            write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"sessionId": sid},
                }
            )
            continue

        if method == "session/prompt":
            sid = params.get("sessionId") or session_id
            # Stream a few agent message chunks
            for chunk in ("Hel", "lo", " OK"):
                write(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": sid,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": chunk},
                                "messageId": "msg-1",
                            },
                        },
                    }
                )

            if want_approval:
                write(
                    {
                        "jsonrpc": "2.0",
                        "id": "srv-perm-1",
                        "method": "session/request_permission",
                        "params": {
                            "sessionId": sid,
                            "toolCall": {
                                "toolCallId": "tool-1",
                                "title": "run ls",
                                "kind": "execute",
                                "status": "pending",
                            },
                            "options": [
                                {
                                    "optionId": "allow-once",
                                    "name": "Allow once",
                                    "kind": "allow_once",
                                },
                                {
                                    "optionId": "allow-always",
                                    "name": "Allow always",
                                    "kind": "allow_always",
                                },
                                {
                                    "optionId": "reject-once",
                                    "name": "Reject",
                                    "kind": "reject_once",
                                },
                            ],
                        },
                    }
                )
                # Wait for client response
                for raw2 in sys.stdin:
                    line2 = raw2.strip()
                    if not line2:
                        continue
                    try:
                        resp = json.loads(line2)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(resp, dict) and resp.get("id") == "srv-perm-1":
                        if decisions_path is not None:
                            decisions.append(resp)
                            Path(decisions_path).write_text(
                                json.dumps(decisions, indent=2), encoding="utf-8"
                            )
                        break
                    if isinstance(resp, dict) and "method" in resp and "id" in resp:
                        write({"jsonrpc": "2.0", "id": resp["id"], "result": {}})

            write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"stopReason": "end_turn"},
                }
            )
            continue

        if method == "session/cancel":
            # notification
            continue

        if method == "session/close":
            write({"jsonrpc": "2.0", "id": req_id, "result": {}})
            continue

        if req_id is not None:
            write({"jsonrpc": "2.0", "id": req_id, "result": {}})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
