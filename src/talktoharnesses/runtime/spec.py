"""Process launch specification — no shell, env override, or path creation."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field

from talktoharnesses.domain._base import FROZEN
from talktoharnesses.domain.models import LaunchSnapshot


class ProcessSpec(BaseModel):
    """Immutable spawn request for a supervised harness process.

    ``argv`` is the adapter-constructed argument tuple **excluding** the
    executable. The resolved executable comes from ``launch.resolved_executable``.
    """

    model_config = FROZEN

    conversation_id: UUID
    binding_id: UUID
    process_id: UUID
    launch: LaunchSnapshot
    argv: tuple[str, ...] = Field(default_factory=tuple)
