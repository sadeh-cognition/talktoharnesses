"""Adapter start/resume enforce published matrices before native work."""

from __future__ import annotations

from uuid import uuid4

import pytest

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import HarnessCapabilities, HarnessConfiguration, LaunchSnapshot
from talktoharnesses.providers.adapter import ResumeSessionRequest, StartSessionRequest
from talktoharnesses.providers.claude.adapter import ClaudeAdapter
from talktoharnesses.providers.claude.compatibility import match_release as match_claude
from talktoharnesses.providers.codex.adapter import CodexAdapter
from talktoharnesses.providers.codex.compatibility import match_release as match_codex
from talktoharnesses.providers.compatibility import (
    CompatibilityMatrixEntry,
    assert_matrix_membership,
)
from talktoharnesses.providers.cursor.adapter import CursorAdapter
from talktoharnesses.providers.cursor.compatibility import match_release as match_cursor
from talktoharnesses.providers.grok.adapter import GrokAdapter
from talktoharnesses.providers.grok.compatibility import match_release as match_grok
from talktoharnesses.providers.opencode.adapter import OpenCodeAdapter
from talktoharnesses.providers.opencode.compatibility import (
    load_opencode_compatibility,
)
from talktoharnesses.providers.opencode.compatibility import (
    match_release as match_opencode,
)


def _launch(kind: HarnessKind) -> LaunchSnapshot:
    return LaunchSnapshot(
        harness_version="1",
        working_directory="/tmp",
        adapter_version="2026.8.0.dev9",
        capabilities=HarnessCapabilities(kind=kind, version="1"),
    )


def _config(kind: HarnessKind) -> HarnessConfiguration:
    return HarnessConfiguration(kind=kind, working_directory="/tmp")


@pytest.mark.asyncio
async def test_adapters_require_probe_before_start() -> None:
    adapters = [
        (GrokAdapter(), HarnessKind.GROK),
        (CursorAdapter(), HarnessKind.CURSOR),
        (CodexAdapter(), HarnessKind.CODEX),
        (ClaudeAdapter(), HarnessKind.CLAUDE),
        (OpenCodeAdapter(), HarnessKind.OPENCODE),
    ]
    for adapter, kind in adapters:
        with pytest.raises(DomainError) as exc:
            await adapter.start(
                StartSessionRequest(
                    conversation_id=uuid4(),
                    binding_id=uuid4(),
                    configuration=_config(kind),
                    launch=_launch(kind),
                )
            )
        assert exc.value.code is ErrorCode.INVALID_STATE


@pytest.mark.asyncio
async def test_adapters_require_probe_before_resume() -> None:
    adapters = [
        (GrokAdapter(), HarnessKind.GROK),
        (CursorAdapter(), HarnessKind.CURSOR),
        (CodexAdapter(), HarnessKind.CODEX),
        (ClaudeAdapter(), HarnessKind.CLAUDE),
        (OpenCodeAdapter(), HarnessKind.OPENCODE),
    ]
    for adapter, kind in adapters:
        with pytest.raises(DomainError) as exc:
            await adapter.resume(
                ResumeSessionRequest(
                    conversation_id=uuid4(),
                    binding_id=uuid4(),
                    configuration=_config(kind),
                    native_session_id="native",
                    launch=_launch(kind),
                )
            )
        assert exc.value.code is ErrorCode.INVALID_STATE


def test_stable_membership_rejects_unlisted_platform() -> None:
    with pytest.raises(DomainError) as exc:
        assert_matrix_membership(
            release_id="x",
            platform="linux",
            matrix=[CompatibilityMatrixEntry(release_id="x", platform="darwin")],
            mode="create",
            harness_label="test",
            package_version="2026.8.0",
        )
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_known_release_helpers_resolve() -> None:
    assert match_grok("grok 1.0.0 (3cd0d0cbce) [stable]", platform="linux").id
    assert match_cursor("2026.08.04-aaa8809", platform="linux").id
    assert match_codex(sdk_version="0.144.4", runtime_version="0.144.4", platform="linux").id
    assert match_claude(
        sdk_version="0.1.53", cli_version="2.1.88", cli_source="bundled", platform="linux"
    ).id
    release = load_opencode_compatibility().releases[0]
    assert match_opencode(release.cli_version, platform="linux").id == release.id
