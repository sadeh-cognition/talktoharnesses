"""Minimal shared SSE decoder for provider streams and the official HTTP client."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SseEvent:
    event: str | None = None
    data: str = ""
    id: str | None = None


@dataclass
class SseDecoder:
    """Incremental UTF-8 SSE parser (LF/CRLF, comments, multi-data)."""

    _buf: bytearray = field(default_factory=bytearray)
    _event: str | None = None
    _data_lines: list[str] = field(default_factory=list[str])
    _id: str | None = None
    max_partial_bytes: int | None = 1_048_576

    def feed(self, chunk: bytes) -> list[SseEvent]:
        if not chunk:
            return []
        # Allow split multi-byte UTF-8 sequences across chunks; full frames are
        # decoded when a blank-line boundary is reached.
        self._buf.extend(chunk)
        if self.max_partial_bytes is not None and len(self._buf) > self.max_partial_bytes:
            raise ValueError("SSE partial frame exceeded max_partial_bytes")
        events: list[SseEvent] = []
        while True:
            found = self._pop_frame()
            if found is None:
                break
            block, _ = found
            try:
                text = block.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("malformed UTF-8 in SSE frame") from exc
            parsed = self._parse_block(text)
            if parsed is not None:
                events.append(parsed)
        return events

    def _pop_frame(self) -> tuple[bytes, int] | None:
        data = bytes(self._buf)
        for marker in (b"\r\n\r\n", b"\n\n"):
            idx = data.find(marker)
            if idx == -1:
                continue
            block = data[:idx]
            consume = idx + len(marker)
            del self._buf[:consume]
            return block, consume
        return None

    def _parse_block(self, text: str) -> SseEvent | None:
        event: str | None = None
        data_lines: list[str] = []
        event_id: str | None = None
        for raw_line in text.splitlines():
            line = raw_line.rstrip("\r")
            if not line:
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line[6:].lstrip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip() if line.startswith("data: ") else line[5:])
            elif line.startswith("id:"):
                event_id = line[3:].lstrip()
            else:
                # Unknown field name — ignore per SSE; OpenCode fixtures use known fields.
                continue
        if event is None and not data_lines and event_id is None:
            return None
        return SseEvent(event=event, data="\n".join(data_lines), id=event_id)
