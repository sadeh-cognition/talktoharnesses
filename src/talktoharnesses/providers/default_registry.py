"""Default production adapter registry construction."""

from __future__ import annotations

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.providers.claude import ClaudeAdapter
from talktoharnesses.providers.codex import CodexAdapter
from talktoharnesses.providers.cursor import CursorAdapter
from talktoharnesses.providers.grok import GrokAdapter
from talktoharnesses.providers.opencode import OpenCodeAdapter
from talktoharnesses.providers.prime_agent import PrimeAgentAdapter
from talktoharnesses.providers.registry import AdapterRegistry


def build_default_adapter_registry() -> AdapterRegistry:
    """Return a registry with every supported adapter."""
    registry = AdapterRegistry()
    registry.register(HarnessKind.GROK, GrokAdapter)
    registry.register(HarnessKind.CURSOR, CursorAdapter)
    registry.register(HarnessKind.CODEX, CodexAdapter)
    registry.register(HarnessKind.CLAUDE, ClaudeAdapter)
    registry.register(HarnessKind.OPENCODE, OpenCodeAdapter)
    registry.register(HarnessKind.PRIME_AGENT, PrimeAgentAdapter)
    return registry
