"""Public sandbox policy contracts. Policies contain references, never credentials."""

from __future__ import annotations

import ipaddress
import re
from pathlib import PurePosixPath
from typing import Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tth_types.enums import HarnessKind


class PolicyModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EgressRule(PolicyModel):
    """An exact public HTTPS host and a path subtree; no unrestricted tunnels."""

    host: str
    path: str = "/"
    methods: tuple[Literal["GET", "HEAD", "POST"], ...] = ("GET", "HEAD")

    @field_validator("host")
    @classmethod
    def public_hostname(cls, value: str) -> str:
        value = value.lower().rstrip(".")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value):
            raise ValueError("Use an exact hostname, without wildcards, ports, or URLs.")
        if "." not in value or any(not part or len(part) > 63 for part in value.split(".")):
            raise ValueError("Use a public fully qualified hostname.")
        try:
            ipaddress.ip_address(value)
        except ValueError:
            pass
        else:
            raise ValueError("IP literals are not permitted in egress rules.")
        if value.endswith((".localhost", ".local", ".internal", ".invalid")):
            raise ValueError("Local hostnames are not permitted in egress rules.")
        return value

    @field_validator("path")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            not value.startswith("/")
            or value.startswith("//")
            or parts.query
            or parts.fragment
            or "%" in value
            or "\\" in value
            or ".." in value.split("/")
        ):
            raise ValueError("Use an absolute URL path without encoding, query, or traversal.")
        return value

    @field_validator("methods")
    @classmethod
    def nonempty_methods(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("Choose at least one method, without duplicates.")
        return value


class CommandRule(PolicyModel):
    """Deny a command's literal argument prefix after shell parsing."""

    argv: tuple[str, ...] = Field(min_length=1)

    @field_validator("argv")
    @classmethod
    def literal_arguments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not arg or "\0" in arg for arg in value):
            raise ValueError("Command arguments must be nonempty and contain no NUL.")
        return value


class SandboxPolicy(PolicyModel):
    """The editable rules for one project. Hard isolation cannot be disabled."""

    project_root: str
    read_only_roots: tuple[str, ...] = ()
    egress: tuple[EgressRule, ...] = (
        EgressRule(host="pypi.org"),
        EgressRule(host="files.pythonhosted.org"),
        EgressRule(host="registry.npmjs.org"),
    )
    providers: tuple[HarnessKind, ...] = tuple(HarnessKind)
    command_rules: tuple[CommandRule, ...] = ()

    @field_validator("project_root")
    @classmethod
    def workspace_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.is_absolute() or str(path) == "/" or ".." in path.parts:
            raise ValueError("Project root must be an absolute directory below root.")
        return str(path)

    @field_validator("read_only_roots")
    @classmethod
    def dependency_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(cls.workspace_path(path) for path in value)


class SandboxPolicyRef(PolicyModel):
    id: UUID
    revision: int = Field(ge=1)


class SandboxPolicyRevision(PolicyModel):
    ref: SandboxPolicyRef
    policy: SandboxPolicy
    repository_directory: str | None = None


class SaveSandboxPolicy(PolicyModel):
    policy: SandboxPolicy
    expected_revision: int = Field(ge=0)


class CommandCheck(PolicyModel):
    command: str = Field(max_length=131072)
    cwd: str


class CommandDecision(PolicyModel):
    allowed: bool
    reason: str | None = None

    @model_validator(mode="after")
    def denial_has_reason(self) -> Self:
        if self.allowed == (self.reason is not None):
            raise ValueError("Only a denied command has a reason.")
        return self


CommandGuardCoverage = Literal["pre_execution", "approval_only", "unavailable"]


def command_guard_coverage(
    kind: HarnessKind, *, yolo: bool, policy: SandboxPolicyRef | None
) -> CommandGuardCoverage:
    if policy is None:
        return "unavailable"
    if kind is HarnessKind.CLAUDE:
        return "pre_execution"
    return "unavailable" if yolo else "approval_only"
