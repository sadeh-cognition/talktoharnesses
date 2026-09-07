"""Logging helpers shared by the split services."""

from __future__ import annotations

import logging
import time


class UtcFormatter(logging.Formatter):
    """Stamp ``%(asctime)s`` in UTC regardless of the process time zone.

    The split log format appends a literal ``Z``; this formatter makes that
    suffix true on its own rather than relying on Django having set ``TZ``
    from ``TIME_ZONE`` at setup.
    """

    converter = staticmethod(time.gmtime)
