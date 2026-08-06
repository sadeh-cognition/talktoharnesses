"""Error hierarchy for talktoharnesses.

Transports and drivers map OS / protocol failures into these types so callers
can handle failures without knowing which harness is underneath.
"""

from __future__ import annotations


class TalkToHarnessesError(Exception):
    """Base error for the package."""


class UnknownHarnessError(TalkToHarnessesError):
    """Raised when ``harness(name)`` is given an unregistered name."""

    def __init__(self, name: str, available: list[str]) -> None:
        self.name = name
        self.available = available
        super().__init__(
            f"Unknown harness {name!r}. Available: {', '.join(available) or '(none)'}"
        )


class MissingDependencyError(TalkToHarnessesError):
    """Raised when a driver needs an optional package that is not installed."""

    def __init__(self, harness: str, package: str, extra: str | None = None) -> None:
        self.harness = harness
        self.package = package
        self.extra = extra
        hint = f' (install with: pip install "talktoharnesses[{extra}]")' if extra else ""
        super().__init__(
            f"Harness {harness!r} requires {package!r}{hint}"
        )


class TransportError(TalkToHarnessesError):
    """Process or wire-level transport failure."""


class ProcessError(TransportError):
    """Subprocess failed to start, exited unexpectedly, or timed out on teardown."""

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        stderr: str | None = None,
    ) -> None:
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(message)


class ProtocolError(TalkToHarnessesError):
    """JSON-RPC / ACP / HTTP protocol violation or unexpected payload."""


class SessionError(TalkToHarnessesError):
    """Session lifecycle error (not started, already stopped, invalid state)."""


class ApprovalError(TalkToHarnessesError):
    """Approval / user-input response targeting an unknown or resolved request."""


class TimeoutError(TalkToHarnessesError):  # noqa: A001 — deliberate shadow of builtin
    """Operation exceeded its deadline."""


class HarnessRuntimeError(TalkToHarnessesError):
    """Provider reported a runtime error that maps to ``runtime.error``."""
