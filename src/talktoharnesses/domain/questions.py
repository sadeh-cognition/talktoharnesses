"""Provider-neutral structured-question normalization and answer validation."""

from __future__ import annotations

from typing import Any, cast

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import (
    CanonicalQuestion,
    CanonicalQuestionOption,
    InteractionAnswer,
)


def canonical_questions(questions: list[dict[str, Any]]) -> tuple[CanonicalQuestion, ...]:
    normalized: list[CanonicalQuestion] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(questions):
        question_id = str(raw.get("id") or f"question-{index + 1}").strip()
        prompt = str(
            raw.get("question") or raw.get("prompt") or raw.get("title") or raw.get("header") or ""
        ).strip()
        if not question_id or not prompt or question_id in seen_ids:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                "structured question requires a unique id and nonblank prompt",
            )
        seen_ids.add(question_id)
        options: list[CanonicalQuestionOption] = []
        option_values: set[str] = set()
        options_obj = raw.get("options")
        if isinstance(options_obj, list):
            for option_obj in cast(list[object], options_obj):
                if isinstance(option_obj, str):
                    label = option_obj.strip()
                    description = ""
                    value = label
                elif isinstance(option_obj, dict):
                    option = cast(dict[object, object], option_obj)
                    label = str(option.get("label") or option.get("value") or "").strip()
                    value = str(option.get("value") or label).strip()
                    description = str(option.get("description") or "").strip()
                else:
                    continue
                if not label or not value:
                    continue
                if value in option_values:
                    raise DomainError(
                        ErrorCode.PROTOCOL_ERROR,
                        f"structured question {question_id!r} has duplicate option values",
                    )
                option_values.add(value)
                options.append(
                    CanonicalQuestionOption(
                        label=label,
                        value=value,
                        description=description or None,
                    )
                )
        header = str(raw.get("header") or "").strip()
        normalized.append(
            CanonicalQuestion(
                id=question_id,
                question=prompt,
                options=tuple(options),
                multiSelect=bool(raw.get("multiSelect") or raw.get("multi_select")),
                header=header or None,
                allowOther=bool(
                    raw.get("allowOther") or raw.get("allow_other") or raw.get("isOther")
                ),
                isSecret=bool(raw.get("isSecret") or raw.get("is_secret")),
            )
        )
    if not normalized:
        raise DomainError(ErrorCode.PROTOCOL_ERROR, "structured question list is empty")
    return tuple(normalized)


def canonical_answer_values(
    answer: InteractionAnswer,
    questions: tuple[CanonicalQuestion, ...],
) -> dict[str, list[str]]:
    if not isinstance(answer.answers, dict):
        raise DomainError(ErrorCode.INVALID_STATE, "structured question requires keyed answers")
    values = cast(dict[object, object], answer.answers)
    expected_ids = {question.id for question in questions}
    provided_ids = {str(question_id) for question_id in values}
    if provided_ids != expected_ids:
        raise DomainError(
            ErrorCode.INVALID_STATE,
            "structured question answers must match the pending question ids",
        )
    normalized: dict[str, list[str]] = {}
    for question in questions:
        question_id = question.id
        raw = values.get(question_id)
        if isinstance(raw, str):
            selected = [raw.strip()] if raw.strip() else []
        elif isinstance(raw, list):
            selected = [str(item).strip() for item in cast(list[object], raw) if str(item).strip()]
        else:
            selected = []
        if not selected:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                f"structured question {question_id!r} requires an answer",
            )
        if not question.multi_select and len(selected) != 1:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                f"structured question {question_id!r} accepts one answer",
            )
        option_values = {option.value for option in question.options}
        if option_values and not question.allow_other and not set(selected).issubset(option_values):
            raise DomainError(
                ErrorCode.INVALID_STATE,
                f"structured question {question_id!r} received an invalid option",
            )
        normalized[question_id] = selected
    return normalized
