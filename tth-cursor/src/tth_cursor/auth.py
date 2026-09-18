"""Shared-secret header auth between the proxy and this split service.

When ``TTH_SPLIT_TOKEN`` is unset the service is open (bare-metal development);
inside a Docker sandbox the proxy always injects a token at container create.
"""

from __future__ import annotations

import hmac

from django.http import HttpRequest
from ninja.security import APIKeyHeader

from tth_cursor.shared.private_env import split_token


class SplitTokenAuth(APIKeyHeader):
    param_name = "X-TTH-Split-Token"

    def authenticate(self, request: HttpRequest, key: str | None) -> str | None:
        expected = split_token()
        if not expected:
            return "open"
        if key is not None and hmac.compare_digest(key, expected):
            return "proxy"
        return None
