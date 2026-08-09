"""Derived-title helper: word count, whitespace, and empty-input behavior."""

from __future__ import annotations

import pytest

from talktoharnesses.application.titles import derive_title_from_user_message


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hello world", "Hello world"),
        (
            "one two three four five six seven eight nine ten",
            "one two three four five six seven eight",
        ),
        ("  leading   and   internal   whitespace  ", "leading and internal whitespace"),
        ("word\nacross\tlines", "word across lines"),
        ("", None),
        ("   ", None),
    ],
)
def test_derive_title_from_user_message(text: str, expected: str | None) -> None:
    assert derive_title_from_user_message(text) == expected


def test_derive_title_caps_at_eight_words() -> None:
    text = " ".join(f"w{i}" for i in range(20))
    title = derive_title_from_user_message(text)
    assert title is not None
    assert title.split() == [f"w{i}" for i in range(8)]
