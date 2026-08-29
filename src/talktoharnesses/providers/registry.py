"""Fixed adapter registry keyed by HarnessKind (no plugin discovery)."""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from typing import cast

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.providers.adapter import HarnessAdapter

AdapterFactory = Callable[[], HarnessAdapter]


async def release_probe_adapter(adapter: HarnessAdapter) -> None:
    """Release a probe-only adapter's resources (duck-typed ``aclose``).

    Probe callers never create a session, so ``close(session)`` does not
    apply; without this, each probe leaks the remote adapter's HTTP client.
    """
    aclose = getattr(adapter, "aclose", None)
    if callable(aclose):
        with contextlib.suppress(Exception):
            await cast("Callable[[], Awaitable[None]]", aclose)()


class AdapterRegistry:
    """Instance-isolated registry of harness adapter factories.

    Each ``create(kind)`` call returns a fresh ``HarnessAdapter`` so runtimes
    never share adapter or SDK objects across conversations.
    """

    def __init__(self) -> None:
        self._factories: dict[HarnessKind, AdapterFactory] = {}

    def register(self, kind: HarnessKind, factory: AdapterFactory) -> None:
        if kind in self._factories:
            raise DomainError(
                ErrorCode.DUPLICATE_REGISTRATION,
                f"adapter already registered for {kind}",
                details={"kind": kind.value},
            )
        self._factories[kind] = factory

    def create(self, kind: HarnessKind) -> HarnessAdapter:
        try:
            factory = self._factories[kind]
        except KeyError as exc:
            raise DomainError(
                ErrorCode.HARNESS_NOT_REGISTERED,
                f"no adapter registered for {kind}",
                details={"kind": kind.value},
            ) from exc
        return factory()

    def kinds(self) -> frozenset[HarnessKind]:
        return frozenset(self._factories)

    def __contains__(self, kind: object) -> bool:
        return isinstance(kind, HarnessKind) and kind in self._factories
