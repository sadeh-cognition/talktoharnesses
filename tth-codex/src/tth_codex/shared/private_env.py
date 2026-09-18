"""Keep split-private process configuration out of harness child environments.

Every harness this split launches, whether through the process supervisor or
an SDK that spawns its own CLI, inherits ``os.environ``. Two entries there are
the split's own business and actively harmful in an agent's shell: the
proxy's shared secret, and ``DJANGO_SETTINGS_MODULE``, which would hijack any
Django project's ``manage.py`` (they ``setdefault`` it). ``seal`` moves them
into this module once Django is configured; readers use the accessors.
"""

from __future__ import annotations

import os

_PRIVATE_NAMES: tuple[str, ...] = ("TTH_SPLIT_TOKEN", "DJANGO_SETTINGS_MODULE")
_sealed: dict[str, str] = {}


def seal() -> None:
    """Move the private variables out of ``os.environ``; idempotent.

    Call after Django settings are configured: the settings module name is
    read lazily, and nothing re-reads it once the settings object exists.
    """
    for name in _PRIVATE_NAMES:
        value = os.environ.pop(name, None)
        if value is not None:
            _sealed[name] = value


def split_token() -> str | None:
    """The proxy's shared secret, sealed or (before sealing, and in tests) live."""
    sealed = _sealed.get("TTH_SPLIT_TOKEN")
    if sealed is not None:
        return sealed
    return os.environ.get("TTH_SPLIT_TOKEN")
