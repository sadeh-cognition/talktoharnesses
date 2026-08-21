"""Prime Agent RPC event normalization."""

from uuid import uuid4

from talktoharnesses.domain.events import ToolRequestedPayload, UsageUpdatedPayload
from talktoharnesses.providers.prime_agent.normalizer import PrimeAgentNormalizer


def test_tool_stream_is_delta_normalized() -> None:
    normalizer = PrimeAgentNormalizer()
    normalizer.begin_turn(uuid4())
    started = normalizer.on_event(
        {
            "type": "tool_execution_start",
            "toolCallId": "call-1",
            "toolName": "ipython",
            "args": {"code": "print('ok')"},
        }
    )
    first = normalizer.on_event(
        {
            "type": "tool_execution_update",
            "toolCallId": "call-1",
            "partialResult": {"content": [{"type": "text", "text": "o"}]},
        }
    )
    second = normalizer.on_event(
        {
            "type": "tool_execution_end",
            "toolCallId": "call-1",
            "result": {"content": [{"type": "text", "text": "ok"}]},
            "isError": False,
        }
    )
    assert [event.type for event in started] == ["tool_requested", "tool_started"]
    assert [event.text for event in first if event.type == "tool_output_delta"] == ["o"]
    assert [event.text for event in second if event.type == "tool_output_delta"] == ["k"]
    assert second[-1].type == "tool_completed"


def test_separate_assistant_messages_get_separate_ids() -> None:
    normalizer = PrimeAgentNormalizer()
    normalizer.begin_turn(uuid4())
    first = normalizer.on_event(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "before"},
        }
    )
    normalizer.on_event({"type": "message_end", "message": {"role": "assistant"}})
    second = normalizer.on_event(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "after"},
        }
    )
    assert first[0].type == "assistant_message_started"
    assert second[0].type == "assistant_message_started"
    assert first[0].message_id != second[0].message_id


def test_assistant_message_usage_is_deduped_and_aggregated_before_terminal() -> None:
    normalizer = PrimeAgentNormalizer()
    turn_id = uuid4()
    normalizer.begin_turn(turn_id)
    first = {
        "type": "message_end",
        "message": {
            "id": "message-1",
            "role": "assistant",
            "usage": {
                "input": 10,
                "output": 2,
                "cacheRead": 4,
                "cacheWrite": 3,
                "totalTokens": 12,
            },
        },
    }
    normalizer.on_event(first)
    normalizer.on_event(first)
    normalizer.on_event(
        {
            "type": "message_end",
            "message": {
                "id": "message-2",
                "role": "assistant",
                "usage": {
                    "input": 20,
                    "output": 5,
                    "cacheRead": 6,
                    "cacheWrite": 7,
                    "totalTokens": 25,
                },
            },
        }
    )
    normalizer.on_event(
        {
            "type": "message_end",
            "message": {"id": "user-1", "role": "user", "usage": {"input": 999}},
        }
    )

    terminal = normalizer.on_event({"type": "agent_end"})
    usage = next(event for event in terminal if isinstance(event, UsageUpdatedPayload))
    assert usage.input_tokens == 30
    assert usage.output_tokens == 7
    assert usage.cached_input_tokens == 10
    assert usage.total_tokens == 37
    assert terminal.index(usage) < len(terminal) - 1


def test_errors_and_tool_arguments_are_redacted() -> None:
    normalizer = PrimeAgentNormalizer()
    normalizer.set_redaction_patterns(("SECRET",))
    turn_id = uuid4()
    normalizer.begin_turn(turn_id)

    warning = normalizer.on_event({"type": "extension_error", "error": "extension leaked SECRET"})
    requested = normalizer.on_event(
        {
            "type": "tool_execution_start",
            "toolCallId": "call-1",
            "toolName": "example",
            "args": {"SECRET-key": ["prefix-SECRET-suffix", {"token": "SECRET"}]},
        }
    )
    pending = normalizer.on_event(
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "error",
                "reason": "error",
                "error": "assistant leaked SECRET",
            },
        }
    )
    terminal = normalizer.on_event({"type": "agent_end"})

    assert warning[0].message == "extension leaked ***"  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    tool = next(event for event in requested if isinstance(event, ToolRequestedPayload))
    assert tool.arguments == {"***-key": ["prefix-***-suffix", {"token": "***"}]}
    assert pending == []
    assert terminal[-1].type == "turn_failed"
    assert terminal[-1].message == "assistant leaked ***"  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]


def test_auto_retry_keeps_turn_active_until_success() -> None:
    normalizer = PrimeAgentNormalizer()
    normalizer.begin_turn(uuid4())

    normalizer.on_event(
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "error",
                "reason": "error",
                "error": "rate limited",
            },
        }
    )
    assert normalizer.turn_active is True
    assert normalizer.on_event({"type": "auto_retry_start"}) == []
    retried = normalizer.on_event(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "recovered"},
        }
    )
    terminal = normalizer.on_event({"type": "agent_end"})

    assert [event.type for event in retried] == [
        "assistant_message_started",
        "assistant_message_delta",
    ]
    assert terminal[-1].type == "turn_completed"
