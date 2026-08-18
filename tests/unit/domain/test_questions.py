"""Provider-neutral structured-question contract."""

from uuid import uuid4

import pytest

from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import InteractionAnswer
from talktoharnesses.domain.questions import canonical_answer_values, canonical_questions


def test_canonical_answers_reject_unknown_option_values() -> None:
    questions = canonical_questions(
        [
            {
                "id": "style",
                "question": "Choose a style",
                "options": [{"label": "Brief", "value": "brief"}],
            }
        ]
    )

    with pytest.raises(DomainError, match="received an invalid option"):
        canonical_answer_values(
            InteractionAnswer(
                interaction_id=uuid4(),
                answers={"style": ["detailed"]},
            ),
            questions,
        )


def test_canonical_answers_allow_codex_other_write_in() -> None:
    questions = canonical_questions(
        [
            {
                "id": "scope",
                "question": "Choose or enter a scope",
                "options": [{"label": "Repository", "value": "repository"}],
                "isOther": True,
                "isSecret": True,
            }
        ]
    )

    assert questions[0].allow_other is True
    assert questions[0].is_secret is True
    assert canonical_answer_values(
        InteractionAnswer(
            interaction_id=uuid4(),
            answers={"scope": "private-scope"},
        ),
        questions,
    ) == {"scope": ["private-scope"]}
