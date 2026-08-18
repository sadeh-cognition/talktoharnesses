"""Typed pending reverse-request state shared by ACP adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from talktoharnesses.domain.models import CanonicalQuestion


@dataclass(frozen=True, slots=True)
class PendingAcpApproval:
    rpc_id: str | int
    options: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class PendingAcpQuestion:
    rpc_id: str | int
    questions: tuple[CanonicalQuestion, ...]


PendingAcpInteraction = PendingAcpApproval | PendingAcpQuestion
