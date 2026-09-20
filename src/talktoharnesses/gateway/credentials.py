"""Host credentials represented by scoped, externally useless handles in sandboxes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, Field
from tth_types.enums import HarnessKind

from talktoharnesses.gateway.routes import (
    KIND_PROVIDERS,
    META_API_HOST,
    META_GATEWAY_BASE,
    TokenExchange,
)


class CursorLoginTokens(BaseModel):
    """Only these fields from Cursor's login response reach the native client."""

    accessToken: str = Field(min_length=1, repr=False)
    refreshToken: str = Field(min_length=1, repr=False)


_SECRET_KEYS = frozenset(
    {
        "token",
        "access",
        "refresh",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "apikey",
        "key",
        "openaiapikey",
        "anthropicapikey",
        "clientsecret",
        "secret",
        "cookie",
        "authorization",
    }
)
_METADATA_KEYS = frozenset(
    {
        "type",
        "authtype",
        "authmode",
        "tokentype",
        "scope",
        "scopes",
        "email",
        "name",
        "accountid",
        "userid",
        "organizationid",
        "orgid",
        "id",
        "provider",
        "providerid",
        "expiresat",
        "expires",
        "expiry",
        "expiration",
        "expiresin",
        "lastrefresh",
        "updatedat",
        "createdat",
        "endpoint",
        "baseurl",
        "apiurl",
        "url",
        "clientid",
        "audience",
        "region",
        "subscriptiontype",
        "subscription",
        "ratelimittier",
        "billingtype",
        "plan",
        "version",
        "apibaseurl",
        "mechanism",
        "obtainedvia",
        "useremail",
        "userfullname",
        "createtime",
        "firstname",
        "profileimageassetid",
        "principaltype",
        "principalid",
        "teamid",
        "teamname",
        "teamrole",
        "oidcissuer",
        "oidcclientid",
        "frontendurl",
        "inferenceurl",
        "sshkeypath",
        "currentenvironment",
    }
)


def key_name(value: str) -> str:
    return value.lower().replace("_", "").replace("-", "")


class UnsupportedCredential(ValueError):
    """The native credential format has not been admitted by the gateway."""


class CredentialVault:
    def __init__(
        self,
        *,
        kind: HarnessKind,
        seed: str,
        auth_file: Path | None = None,
        api_keys: dict[str, str] | None = None,
        cursor_login_file: Path | None = None,
    ) -> None:
        self.kind = kind
        self.seed = seed.encode()
        self.auth_file = auth_file
        self.api_keys = api_keys or {}
        self.cursor_login_file = cursor_login_file
        self._handles: dict[str, tuple[str, str, tuple[str, ...]]] = {}

    def _provider(self, path: tuple[str, ...]) -> str:
        if self.kind in {HarnessKind.OPENCODE, HarnessKind.PRIME_AGENT}:
            for part in path:
                part = part.lower().removesuffix("_api_key")
                normalized = {
                    "codex": "openai",
                    "claude": "anthropic",
                    "grok": "xai",
                    "prime-inference": "prime",
                }.get(part, part)
                if normalized in KIND_PROVIDERS[self.kind]:
                    return normalized
            if self.kind is HarnessKind.OPENCODE:
                raise UnsupportedCredential("The OpenCode credential provider is not supported.")
            return "prime"
        return next(iter(KIND_PROVIDERS[self.kind]))

    def _signature(self, path: tuple[str, ...], jwt_prefix: str = "") -> str:
        payload = json.dumps([path, jwt_prefix]).encode()
        return "tth_" + hmac.new(self.seed, payload, hashlib.sha256).hexdigest()

    def _handle(self, value: str, path: tuple[str, ...]) -> str:
        identifier = self._signature(path)
        provider = self._provider(path)
        # Native clients can inspect JWT account/expiry claims without receiving
        # a provider-valid signature. Authorization is substituted at the gateway.
        parts = value.split(".")
        if len(parts) == 3:
            try:
                json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
                prefix = f"{parts[0]}.{parts[1]}"
                identifier = prefix + "." + self._signature(path, prefix)
            except (ValueError, UnicodeError):
                pass
        self._handles[identifier] = (value, provider, path)
        return identifier

    def _virtualize(self, value: Any, path: tuple[str, ...] = ()) -> Any:
        if isinstance(value, dict):
            return {
                str(key): self._virtualize(child, (*path, str(key)))
                for key, child in cast(dict[str, Any], value).items()
            }
        if isinstance(value, list):
            return [
                self._virtualize(child, (*path, str(index)))
                for index, child in enumerate(cast(list[Any], value))
            ]
        if isinstance(value, str) and value:
            key = key_name(next((part for part in reversed(path) if not part.isdigit()), ""))
            if key in _SECRET_KEYS:
                return self._handle(value, path)
            if self.kind is HarnessKind.MUSE and key == "apibaseurl":
                if value.rstrip("/") != f"https://{META_API_HOST}/v1":
                    raise UnsupportedCredential("The Muse API origin is not supported.")
                # Muse's inference transport uses bundled roots. Its native
                # base URL supports a fixed reverse proxy on the private network.
                return META_GATEWAY_BASE
            if key not in _METADATA_KEYS:
                raise UnsupportedCredential(
                    "The native credential file contains an unsupported field."
                )
        return value

    def snapshot(self) -> tuple[dict[str, str], Any | None]:
        self._handles.clear()
        env = {name: self._handle(value, (name,)) for name, value in self.api_keys.items()}
        document = None
        if self.auth_file is not None:
            document = self._virtualize(json.loads(self.auth_file.read_text()))
        if self.cursor_login_file is not None and self.cursor_login_file.exists():
            login = CursorLoginTokens.model_validate_json(self.cursor_login_file.read_text())
            self._virtualize(login.model_dump(), ("cursor_login",))
        if self.kind is HarnessKind.MUSE and document is None and "META_API_KEY" in env:
            document = {
                "schema_version": 1,
                "providers": {
                    "meta": {
                        "mechanism": "api_key",
                        "api_key": env["META_API_KEY"],
                        "api_base_url": META_GATEWAY_BASE,
                    }
                },
            }
        if self.kind is HarnessKind.MUSE and document is not None:
            if not isinstance(document, dict):
                raise UnsupportedCredential("The Muse credential file is not supported.")
            document = cast(dict[str, Any], document)
            providers = document.get("providers")
            meta = (
                cast(dict[str, Any], providers).get("meta") if isinstance(providers, dict) else None
            )
            if not isinstance(meta, dict):
                raise UnsupportedCredential("The Muse credential file is not supported.")
            cast(dict[str, Any], meta)["api_base_url"] = META_GATEWAY_BASE
        if not self._handles:
            raise UnsupportedCredential("No supported host credential was configured.")
        return env, document

    def substitute(self, value: str, provider: str) -> str:
        """Only call for an admitted authentication field, never a prompt body."""
        record = self._handles.get(value)
        if record is None and value.count(".") == 2:
            # A different scope may have rotated the host JWT. Authenticate
            # the previously issued claims, then use the current host token.
            prefix, signature = value.rsplit(".", 1)
            record = next(
                (
                    candidate
                    for candidate in self._handles.values()
                    if candidate[1] == provider
                    and hmac.compare_digest(signature, self._signature(candidate[2], prefix))
                ),
                None,
            )
        if record is None or record[1] != provider:
            raise UnsupportedCredential("The credential handle does not belong to this provider.")
        return record[0]

    def authentication_header(self, value: str, provider: str) -> str:
        if value.lower().startswith("bearer "):
            return "Bearer " + self.substitute(value[7:], provider)
        if value.lower().startswith("basic "):
            decoded = base64.b64decode(value[6:], validate=True).decode()
            username, separator, password = decoded.partition(":")
            if not separator:
                raise UnsupportedCredential("Invalid Basic authentication.")
            raw = username + ":" + self.substitute(password, provider)
            return "Basic " + base64.b64encode(raw.encode()).decode()
        return self.substitute(value, provider)

    def refresh_request(self, document: Any, provider: str) -> Any:
        if not isinstance(document, dict):
            raise UnsupportedCredential("Unsupported token exchange format.")
        result = dict(cast(dict[str, Any], document))
        for key, value in result.items():
            if key_name(key) in _SECRET_KEYS and isinstance(value, str):
                result[key] = self.substitute(value, provider)
        return result

    def exchange_file(self, exchange: TokenExchange) -> Path:
        path = self.cursor_login_file if exchange is TokenExchange.CURSOR_LOGIN else self.auth_file
        if path is None:
            raise UnsupportedCredential("Host credential storage is required for this exchange.")
        return path

    def refreshed(
        self, document: Any, provider: str, *, exchange: TokenExchange = TokenExchange.REFRESH
    ) -> Any:
        """Persist token rotation and return only handles to the native client.

        The caller holds the credential file's cross-process refresh lock across
        both the upstream request and this update.
        """
        if exchange is TokenExchange.CURSOR_LOGIN:
            login = CursorLoginTokens.model_validate(document)
            path = self.exchange_file(exchange)
            atomic_json(path, login.model_dump())
            return self._virtualize(login.model_dump(), ("cursor_login",))
        if not isinstance(document, dict) or self.auth_file is None:
            raise UnsupportedCredential("Unsupported token refresh response.")
        native = json.loads(self.auth_file.read_text())
        paths = {
            key_name(path[-1]): path
            for _, audience, path in self._handles.values()
            if audience == provider and len(path) > 0
        }

        def replace(value: Any, key: str = "") -> Any:
            if isinstance(value, dict):
                return {
                    name: replace(child, name)
                    for name, child in cast(dict[str, Any], value).items()
                }
            if isinstance(value, list):
                return [replace(child, key) for child in cast(list[Any], value)]
            normalized = key_name(key)
            if not isinstance(value, str) or not value:
                return value
            if normalized not in _SECRET_KEYS:
                if normalized not in _METADATA_KEYS:
                    raise UnsupportedCredential("Unsupported token exchange response field.")
                return value
            aliases = {"accesstoken": ("access", "key"), "refreshtoken": ("refresh",)}
            path = paths.get(normalized) or next(
                (paths[alias] for alias in aliases.get(normalized, ()) if alias in paths), None
            )
            if path is None:
                raise UnsupportedCredential("The refresh response introduced an unsupported token.")
            parent = native
            for part in path[:-1]:
                parent = parent[part]
            parent[path[-1]] = value
            return self._handle(value, path)

        output = replace(document)
        lifetime = cast(dict[str, Any], document).get("expires_in")
        if isinstance(lifetime, (int, float)):
            for path in paths.values():
                parent = native
                for part in path[:-1]:
                    parent = parent[part]
                for field, value in tuple(parent.items()):
                    if key_name(field) in {"expiresat", "expires"} and isinstance(
                        value, (int, float)
                    ):
                        parent[field] = int(
                            (time.time() + lifetime) * (1000 if value > 1e12 else 1)
                        )
        atomic_json(self.auth_file, native)
        return output


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
