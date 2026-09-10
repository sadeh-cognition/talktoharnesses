"""The turn-usage accumulator every adapter reports through."""

from __future__ import annotations

from uuid import uuid4

from tth_types.usage import TurnUsage


def test_each_report_is_the_turn_total_so_far_not_the_increment() -> None:
    """The canonical payload's whole contract, in one place instead of six."""
    usage = TurnUsage()
    turn = uuid4()

    first = usage.add(turn, input_tokens=10, output_tokens=3)
    assert [(p.input_tokens, p.output_tokens) for p in first] == [(10, 3)]

    second = usage.add(turn, input_tokens=20, output_tokens=5)
    assert [(p.input_tokens, p.output_tokens) for p in second] == [(30, 8)]


def test_a_redelivered_frame_is_not_counted_twice() -> None:
    """A replayed stream or a retried read repeats frames the turn already had."""
    usage = TurnUsage()
    turn = uuid4()

    assert usage.add(turn, frame_id="m1", input_tokens=10)
    assert usage.add(turn, frame_id="m1", input_tokens=10) == []
    assert [p.input_tokens for p in usage.report(turn)] == [10]


def test_terminal_figures_supersede_the_running_total_and_close_the_turn() -> None:
    """A frame trailing the terminal one must not report a smaller total."""
    usage = TurnUsage()
    turn = uuid4()

    usage.add(turn, input_tokens=10, output_tokens=3)
    usage.add(turn, input_tokens=20, output_tokens=5)
    final = usage.replace(turn, input_tokens=31, output_tokens=9, total_tokens=40)
    assert [(p.input_tokens, p.output_tokens, p.total_tokens) for p in final] == [(31, 9, 40)]

    assert usage.add(turn, input_tokens=7, output_tokens=1) == []
    assert [p.input_tokens for p in usage.report(turn)] == [31]


def test_a_running_total_trailing_the_terminal_one_is_ignored() -> None:
    """A protocol-level running total must not reopen a turn a provider closed."""
    usage = TurnUsage()
    turn = uuid4()

    usage.replace(turn, input_tokens=31, output_tokens=9)
    assert usage.replace(turn, final=False, input_tokens=5, output_tokens=1) == []
    assert [p.input_tokens for p in usage.report(turn)] == [31]


def test_a_provider_reporting_its_own_running_totals_is_not_closed() -> None:
    """Some providers count cumulatively themselves; each reading supersedes."""
    usage = TurnUsage()
    turn = uuid4()

    usage.replace(turn, final=False, input_tokens=10, output_tokens=3)
    later = usage.replace(turn, final=False, input_tokens=30, output_tokens=8)
    assert [(p.input_tokens, p.output_tokens) for p in later] == [(30, 8)]


def test_a_category_only_some_frames_report_is_left_absent() -> None:
    """Summing the frames that did report would understate the turn."""
    usage = TurnUsage()
    turn = uuid4()

    usage.add(turn, input_tokens=10, output_tokens=3, total_tokens=13)
    payloads = usage.add(turn, input_tokens=20, output_tokens=5)
    assert [(p.input_tokens, p.total_tokens) for p in payloads] == [(30, None)]


def test_only_nonnegative_integers_count() -> None:
    """Booleans, floats, strings and negatives are not token counts."""
    usage = TurnUsage()
    turn = uuid4()

    assert usage.add(turn, input_tokens=True, output_tokens="7", total_tokens=-1) == []
    assert usage.add(turn, input_tokens=1.5, cached_input_tokens=None) == []
    assert usage.reported is False


def test_a_new_turn_starts_from_nothing() -> None:
    usage = TurnUsage()
    first, second = uuid4(), uuid4()

    usage.add(first, input_tokens=10)
    usage.replace(first, input_tokens=12)
    usage.reset()

    assert [p.input_tokens for p in usage.add(second, input_tokens=7)] == [7]
