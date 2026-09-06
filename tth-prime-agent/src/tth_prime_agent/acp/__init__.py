"""Newline-delimited JSON framing for the Prime Agent stream.

Prime Agent speaks JSONL rather than ACP JSON-RPC 2.0, so only the frame
decoder lives here. Nothing is exported as a supported public surface.
"""

from __future__ import annotations

__all__: list[str] = []
