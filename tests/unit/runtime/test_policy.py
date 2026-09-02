"""RuntimePolicy defaults and ``TTH_RUNTIME_*`` environment overrides."""

from __future__ import annotations

import pytest

from talktoharnesses.runtime.policy import IDLE_REAP_ENV, MAX_RUNTIMES_ENV, RuntimePolicy


def test_defaults_reap_idle_runtimes_after_five_minutes() -> None:
    policy = RuntimePolicy()
    assert policy.idle_reap == 5 * 60
    assert policy.max_runtimes == 20


def test_from_env_without_overrides_matches_defaults() -> None:
    assert RuntimePolicy.from_env({}) == RuntimePolicy()
    # Empty values are treated as unset.
    assert RuntimePolicy.from_env({IDLE_REAP_ENV: "", MAX_RUNTIMES_ENV: ""}) == RuntimePolicy()


def test_from_env_reads_idle_reap_and_max_runtimes() -> None:
    policy = RuntimePolicy.from_env({IDLE_REAP_ENV: "42.5", MAX_RUNTIMES_ENV: "3"})
    assert policy.idle_reap == 42.5
    assert policy.max_runtimes == 3
    # Untouched fields keep their defaults.
    assert policy.lease_duration == RuntimePolicy().lease_duration


@pytest.mark.parametrize("raw", ["abc", "0", "-5"])
def test_from_env_rejects_non_positive_idle_reap(raw: str) -> None:
    with pytest.raises(ValueError, match=IDLE_REAP_ENV):
        RuntimePolicy.from_env({IDLE_REAP_ENV: raw})


@pytest.mark.parametrize("raw", ["2.5", "0", "many"])
def test_from_env_rejects_invalid_max_runtimes(raw: str) -> None:
    with pytest.raises(ValueError, match=MAX_RUNTIMES_ENV):
        RuntimePolicy.from_env({MAX_RUNTIMES_ENV: raw})
