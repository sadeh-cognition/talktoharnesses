"""Protocol-complete echo adapter for running the split without the real SDK.

Activate with ``TTH_SPLIT_ADAPTER_FACTORY=tth_cursor.testing:echo_adapter_factory``.
Each submitted turn emits started → assistant message ("echo: <prompt>") →
completed. Used by cross-process integration tests and local development.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

from tth_types.adapter import (
    HarnessInteractionRequest,
    HarnessSession,
    ResumeSessionRequest,
    StartSessionRequest,
    SteerRequest,
    TurnRequest,
)
from tth_types.enums import HarnessKind
from tth_types.events import (
    AssistantMessageCompletedPayload,
    AssistantMessageDeltaPayload,
    AssistantMessageStartedPayload,
    HarnessEvent,
    TurnCompletedPayload,
)
from tth_types.harness import (
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessEffortInfo,
    HarnessModelInfo,
    InteractionAnswer,
)


class EchoAdapter:
    kind = HarnessKind.CURSOR
    sdk_managed = True

    def __init__(self) -> None:
        self._queue: asyncio.Queue[HarnessEvent | HarnessInteractionRequest | None] = (
            asyncio.Queue()
        )
        self._seen: set[str] = set()
        self._session: HarnessSession | None = None

    def set_redaction_patterns(self, patterns: tuple[str, ...]) -> None:
        del patterns

    def import_seen(self, native_ids: frozenset[str], offsets: frozenset[str]) -> None:
        del offsets
        self._seen.update(native_ids)

    def export_seen(self) -> tuple[frozenset[str], frozenset[str]]:
        return frozenset(self._seen), frozenset()

    async def probe(self, config: HarnessConfiguration) -> HarnessCapabilities:
        del config
        return HarnessCapabilities(
            kind=HarnessKind.CURSOR,
            version="0.0.0+echo",
            supports_resume=True,
            supports_interrupt=True,
            models=(HarnessModelInfo(id="echo-model", label="Echo"),),
            efforts=(HarnessEffortInfo(id="high", label="High"),),
        )

    async def start(self, request: StartSessionRequest) -> HarnessSession:
        self._session = HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=HarnessKind.CURSOR,
            native_session_id=str(uuid4()),
            model=request.configuration.model,
        )
        return self._session

    async def resume(self, request: ResumeSessionRequest) -> HarnessSession:
        self._session = HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=HarnessKind.CURSOR,
            native_session_id=request.native_session_id,
        )
        return self._session

    async def submit(self, session: HarnessSession, request: TurnRequest) -> None:
        del session
        self._seen.add(f"echo-{request.turn_id}")
        message_id = uuid4()
        text = f"echo: {request.prompt}"
        # turn_started is proxy-owned; adapters emit only content and terminals.
        await self._queue.put(
            AssistantMessageStartedPayload(turn_id=request.turn_id, message_id=message_id)
        )
        await self._queue.put(
            AssistantMessageDeltaPayload(
                turn_id=request.turn_id,
                message_id=message_id,
                sequence=1,
                text=text,
            )
        )
        await self._queue.put(
            AssistantMessageCompletedPayload(
                turn_id=request.turn_id,
                message_id=message_id,
                text=text,
            )
        )
        await self._queue.put(
            TurnCompletedPayload(turn_id=request.turn_id, has_assistant_message=True)
        )

    async def steer(self, session: HarnessSession, request: SteerRequest) -> bool:
        del session, request
        return False

    async def interrupt(self, session: HarnessSession) -> None:
        del session

    async def answer_interaction(
        self, session: HarnessSession, answer: InteractionAnswer
    ) -> None:
        del session, answer

    def events(
        self, session: HarnessSession
    ) -> AsyncIterator[HarnessEvent | HarnessInteractionRequest]:
        del session

        async def _gen() -> AsyncIterator[HarnessEvent | HarnessInteractionRequest]:
            while True:
                item = await self._queue.get()
                if item is None:
                    return
                yield item

        return _gen()

    async def close(self, session: HarnessSession) -> None:
        del session
        await self._queue.put(None)


def echo_adapter_factory() -> EchoAdapter:
    return EchoAdapter()


class SpawnEchoAdapter(EchoAdapter):
    """Echo adapter that also exercises the supervised-spawn path.

    ``build_argv`` runs a sleeping python child under the split's supervisor
    (point ``TALKTOHARNESSES_CURSOR_EXECUTABLE`` at a python interpreter).
    """

    sdk_managed = False

    def __init__(self) -> None:
        super().__init__()
        self.handle: object | None = None

    def build_argv(self, configuration: HarnessConfiguration) -> tuple[str, ...]:
        del configuration
        return ("-c", "import time; time.sleep(300)")

    def bind_process(self, handle: object) -> None:
        self.handle = handle


def spawn_echo_adapter_factory() -> SpawnEchoAdapter:
    return SpawnEchoAdapter()
