"""Strict OpenAPI-derived OpenCode HTTP/SSE schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

_STRICT = ConfigDict(extra="forbid", frozen=True)


class OpenCodeHealth(BaseModel):
    model_config = _STRICT

    healthy: bool
    version: str


class OpenCodeSessionTime(BaseModel):
    model_config = _STRICT

    created: int | None = None
    updated: int | None = None


class OpenCodeSessionSummary(BaseModel):
    model_config = _STRICT

    additions: int | None = None
    deletions: int | None = None
    files: int | None = None


class OpenCodeSession(BaseModel):
    model_config = _STRICT

    id: str
    title: str | None = None
    directory: str | None = None
    slug: str | None = None
    version: str | None = None
    projectID: str | None = None
    time: OpenCodeSessionTime | None = None
    summary: OpenCodeSessionSummary | None = None


class OpenCodeMessageAck(BaseModel):
    model_config = _STRICT

    id: str
    sessionID: str | None = None
    role: str | None = None


class OpenCodePermissionRequest(BaseModel):
    model_config = _STRICT

    id: str
    sessionID: str
    tool: str | None = None
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class OpenCodeServerEvent(BaseModel):
    model_config = _STRICT

    type: str
    properties: dict[str, Any] = Field(default_factory=dict)


class OpenCodeSessionStatus(BaseModel):
    model_config = _STRICT

    type: Literal["session.status"] = "session.status"
    sessionID: str
    status: str


class OpenCodeMessagePartDelta(BaseModel):
    model_config = _STRICT

    type: Literal["message.part.delta"] = "message.part.delta"
    sessionID: str
    messageID: str
    partID: str
    field: str
    delta: str


class OpenCodePermissionAsked(BaseModel):
    model_config = _STRICT

    type: Literal["permission.asked"] = "permission.asked"
    sessionID: str
    permissionID: str
    tool: str | None = None
    title: str | None = None


class OpenCodeConnected(BaseModel):
    model_config = _STRICT

    type: Literal["server.connected"] = "server.connected"


def parse_server_event(raw: dict[str, Any]) -> OpenCodeServerEvent:
    return OpenCodeServerEvent.model_validate(raw)
