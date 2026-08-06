#!/usr/bin/env python3
"""Echo JSON-RPC peer for transport tests.

Reads newline-delimited JSON-RPC from stdin, writes responses to stdout.

Behaviours (controlled by request method / params):
- default: result = {"echo": params, "method": method}
- "notify-me": after responding, emit a notification ``peer/tick`` with params
- "ping-client": after responding, send a *request* ``client/ping`` and print
  the result as a notification ``peer/ping-result``
- "hang": never respond (for timeout tests)
- "exit": respond then exit 0
"""

from __future__ import annotations

import json
import sys
from typing import Any


def write(msg: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
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

        # Ignore responses to our reverse-requests (just drain).
        if "method" not in msg:
            continue

        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params")

        if method == "hang":
            # Intentionally never respond.
            continue

        if method == "exit":
            if req_id is not None:
                write({"jsonrpc": "2.0", "id": req_id, "result": {"bye": True}})
            return 0

        if req_id is not None:
            write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"echo": params, "method": method},
                }
            )

        if method == "notify-me":
            write(
                {
                    "jsonrpc": "2.0",
                    "method": "peer/tick",
                    "params": {"from": "echo", "payload": params},
                }
            )

        if method == "ping-client" and req_id is not None:
            # Reverse request to the client.
            write(
                {
                    "jsonrpc": "2.0",
                    "id": f"rev-{req_id}",
                    "method": "client/ping",
                    "params": {"n": 1},
                }
            )
            # Wait for the response on a subsequent stdin line (handled below by
            # continuing the loop; when we see a response with matching id we
            # emit peer/ping-result).
            # Simpler approach: block reading until we get the matching response.
            for raw2 in sys.stdin:
                line2 = raw2.strip()
                if not line2:
                    continue
                try:
                    resp = json.loads(line2)
                except json.JSONDecodeError:
                    continue
                if isinstance(resp, dict) and resp.get("id") == f"rev-{req_id}":
                    write(
                        {
                            "jsonrpc": "2.0",
                            "method": "peer/ping-result",
                            "params": {"result": resp.get("result"), "error": resp.get("error")},
                        }
                    )
                    break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
