#!/usr/bin/env python3
"""Codex app-server mock peer.

Replays a canned single-agent turn (initialize → thread/start → turn/start
with agentMessage deltas, optional approval request, turn/completed).

Wire shape matches T3's codexMultiAgentWire.json responses for the happy path,
simplified to one thread.

Env:
  TALKTOHARNESSES_CODEX_FIXTURE — path to canned JSON (optional)
  TALKTOHARNESSES_CODEX_APPROVAL — if "1", raise an approval request mid-turn
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_FIXTURE = Path(__file__).with_name("codex_canned_turn.json")


def write(msg: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def load_fixture() -> dict[str, Any]:
    path = Path(os.environ.get("TALKTOHARNESSES_CODEX_FIXTURE", DEFAULT_FIXTURE))
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    fixture = load_fixture()
    thread_id = fixture["threadId"]
    session_id = fixture.get("sessionId", thread_id)
    turn_id = fixture["turnId"]
    model = fixture.get("model", "gpt-test")
    chunks: list[str] = list(fixture.get("textChunks") or ["OK"])
    want_approval = os.environ.get("TALKTOHARNESSES_CODEX_APPROVAL") == "1"
    approval = fixture.get("approval") or {}

    # Record of decisions returned for approvals (for test assertions via sidecar).
    decisions_path = os.environ.get("TALKTOHARNESSES_CODEX_DECISIONS")
    decisions: list[Any] = []

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

        # Response to our approval request
        if "method" not in msg and "id" in msg:
            # client response to server request
            if decisions_path is not None:
                decisions.append(msg)
                Path(decisions_path).write_text(
                    json.dumps(decisions, indent=2), encoding="utf-8"
                )
            continue

        method = msg.get("method")
        req_id = msg.get("id")

        if method == "initialize":
            write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "userAgent": "talktoharnesses-codex-mock/0.1",
                        "codexHome": "/tmp",
                        "platformFamily": "unix",
                        "platformOs": "linux",
                    },
                }
            )
            continue

        if method == "initialized":
            # notification — no response
            continue

        if method in ("thread/start", "thread/resume"):
            write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "thread": {
                            "id": thread_id,
                            "sessionId": session_id,
                            "status": {"type": "idle"},
                            "turns": [],
                        },
                        "model": model,
                        "cwd": (msg.get("params") or {}).get("cwd"),
                    },
                }
            )
            write(
                {
                    "jsonrpc": "2.0",
                    "method": "thread/started",
                    "params": {
                        "thread": {
                            "id": thread_id,
                            "sessionId": session_id,
                            "status": {"type": "idle"},
                        }
                    },
                }
            )
            continue

        if method == "turn/start":
            turn = {
                "id": turn_id,
                "items": [],
                "status": "inProgress",
                "error": None,
            }
            write({"jsonrpc": "2.0", "id": req_id, "result": {"turn": turn}})
            write(
                {
                    "jsonrpc": "2.0",
                    "method": "turn/started",
                    "params": {"threadId": thread_id, "turn": turn},
                }
            )
            write(
                {
                    "jsonrpc": "2.0",
                    "method": "item/started",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "item": {"type": "agentMessage", "id": "item-msg-1"},
                    },
                }
            )
            for chunk in chunks:
                write(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "itemId": "item-msg-1",
                            "delta": chunk,
                        },
                    }
                )
            write(
                {
                    "jsonrpc": "2.0",
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "item": {
                            "type": "agentMessage",
                            "id": "item-msg-1",
                            "status": "completed",
                        },
                    },
                }
            )

            if want_approval:
                # Mid-turn approval: send server request, wait for response on stdin.
                appr_id = approval.get("requestId", "approval-1")
                write(
                    {
                        "jsonrpc": "2.0",
                        "id": f"srv-{appr_id}",
                        "method": approval.get(
                            "method", "item/commandExecution/requestApproval"
                        ),
                        "params": {
                            "requestId": appr_id,
                            "command": approval.get("command", "ls -la"),
                            "threadId": thread_id,
                            "turnId": turn_id,
                        },
                    }
                )
                # Block until client responds to srv-{appr_id}
                for raw2 in sys.stdin:
                    line2 = raw2.strip()
                    if not line2:
                        continue
                    try:
                        resp = json.loads(line2)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(resp, dict) and resp.get("id") == f"srv-{appr_id}":
                        if decisions_path is not None:
                            decisions.append(resp)
                            Path(decisions_path).write_text(
                                json.dumps(decisions, indent=2), encoding="utf-8"
                            )
                        break
                    # Could be concurrent client requests; ignore/respond empty
                    if isinstance(resp, dict) and "method" in resp and "id" in resp:
                        write(
                            {
                                "jsonrpc": "2.0",
                                "id": resp["id"],
                                "result": {},
                            }
                        )

            write(
                {
                    "jsonrpc": "2.0",
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turn": {**turn, "status": "completed"},
                    },
                }
            )
            continue

        if method == "turn/interrupt":
            write({"jsonrpc": "2.0", "id": req_id, "result": {}})
            write(
                {
                    "jsonrpc": "2.0",
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turn": {
                            "id": (msg.get("params") or {}).get("turnId") or turn_id,
                            "status": "interrupted",
                        },
                    },
                }
            )
            continue

        if req_id is not None:
            write({"jsonrpc": "2.0", "id": req_id, "result": {}})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
