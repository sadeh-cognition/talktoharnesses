"""Optional Django application package for talktoharnesses."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from talktoharnesses.django.persistence import DjangoPersistence

__all__ = ["DjangoPersistence"]


def __getattr__(name: str) -> Any:
    if name == "DjangoPersistence":
        from talktoharnesses.django.persistence import DjangoPersistence

        return DjangoPersistence
    raise AttributeError(name)
