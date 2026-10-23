"""The tick's decisions: when it pings, and what each outcome records, exactly."""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TOOL_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import SUCCESS_RESULT, rate_limit_line, stream  # noqa: E402 - import follows the sys.path bootstrap above
from ping_output import (  # noqa: E402 - same bootstrap
    OUTCOME_FAILED,
    OUTCOME_LIMITED,
    OUTCOME_OK,
    PingOutcome,
    parse_ping_output,
)
from state_store import EMPTY_STATE, PrimerState  # noqa: E402 - same bootstrap
from window_rules import (  # noqa: E402 - same bootstrap
    SOURCE_ESTIMATED,
    SOURCE_MANUAL,
    SOURCE_SERVER,
    WINDOW_END_SLACK,
    WINDOW_LENGTH,
    TickAction,
    apply_outcome,
    decide_tick,
    duration_text,
    next_ping_due,
    parse_user_datetime,
    record_manual_last_run,
    trusted_window_end,
)

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_HOUR = timedelta(hours=1)
_RESET = datetime(2026, 1, 1, 18, 0, tzinfo=UTC)
# Its last attempt succeeded, so the attempt and the success are the same moment.
_PRIOR = PrimerState(
    _NOW - _HOUR, SOURCE_SERVER, _NOW - 6 * _HOUR, "ok", 3, _NOW - 6 * _HOUR
)


class DecideTickTest(unittest.TestCase):
    """The tick table, including the boundary where now equals the window end."""

    def test_decide_tick_follows_the_tick_table(self) -> None:
        rows = [
            ("before start, no window", _NOW + _HOUR, None, TickAction.WAIT_FOR_START),
            (
                "before start, window ended",
                _NOW + _HOUR,
                _NOW - _HOUR,
                TickAction.WAIT_FOR_START,
            ),
            ("no start, no window", None, None, TickAction.PING),
            ("exactly at start, no window", _NOW, None, TickAction.PING),
            ("exactly at the window end", None, _NOW, TickAction.PING),
            ("after the window end", _NOW - _HOUR, _NOW - _HOUR, TickAction.PING),
            ("inside the window", None, _NOW + _HOUR, TickAction.WAIT_FOR_WINDOW_END),
            (
                "start passed, inside the window",
                _NOW - _HOUR,
                _NOW + _HOUR,
                TickAction.WAIT_FOR_WINDOW_END,
            ),
        ]
        for name, start, window_ends_at, expected in rows:
            with self.subTest(row=name):
                self.assertIs(decide_tick(_NOW, start, window_ends_at), expected)


class ApplyOutcomeTest(unittest.TestCase):
    """The outcome table."""

    def test_a_success_with_a_reported_end_records_the_server_end(self) -> None:
        state = apply_outcome(
            _PRIOR, PingOutcome(OUTCOME_OK, _RESET, "ping succeeded"), _NOW
        )
        self.assertEqual(
            state, PrimerState(_RESET, SOURCE_SERVER, _NOW, OUTCOME_OK, 0, _NOW)
        )

    def test_a_success_without_a_reported_end_estimates_five_hours(self) -> None:
        state = apply_outcome(
            _PRIOR, PingOutcome(OUTCOME_OK, None, "ping succeeded"), _NOW
        )
        expected = PrimerState(
            _NOW + WINDOW_LENGTH, SOURCE_ESTIMATED, _NOW, OUTCOME_OK, 0, _NOW
        )
        self.assertEqual(state, expected)

    def test_limited_records_the_reported_end(self) -> None:
        outcome = PingOutcome(OUTCOME_LIMITED, _RESET, "at the usage limit")
        state = apply_outcome(_PRIOR, outcome, _NOW)
        self.assertEqual(
            state,
            PrimerState(_RESET, SOURCE_SERVER, _NOW, OUTCOME_LIMITED, 0, _NOW),
        )

    def test_a_failure_keeps_the_window_and_counts(self) -> None:
        state = apply_outcome(
            _PRIOR, PingOutcome(OUTCOME_FAILED, None, "exit 1: boom"), _NOW
        )
        self.assertEqual(state.window_ends_at, _PRIOR.window_ends_at)
        self.assertEqual(state.window_end_source, _PRIOR.window_end_source)
        # The last success stays where it was; only the attempt moves to this ping.
        self.assertEqual(state.last_ping_at, _PRIOR.last_ping_at)
        self.assertEqual(state.last_attempt_at, _NOW)
        self.assertEqual(
            (state.last_outcome, state.consecutive_failures), (OUTCOME_FAILED, 4)
        )

    def test_a_manual_last_run_sets_a_five_hour_end(self) -> None:
        last_run = _NOW - 2 * _HOUR
        state = record_manual_last_run(EMPTY_STATE, last_run)
        self.assertEqual(state.window_ends_at, last_run + WINDOW_LENGTH)
        self.assertEqual(
            (state.window_end_source, state.last_ping_at), (SOURCE_MANUAL, last_run)
        )

    def test_a_manual_last_run_is_not_an_attempt(self) -> None:
        # An operator-reported ping was not sent by this tool, so the attempt stays put.
        state = record_manual_last_run(_PRIOR, _NOW - 2 * _HOUR)
        self.assertEqual(state.last_attempt_at, _PRIOR.last_attempt_at)


class TrustedWindowEndTest(unittest.TestCase):
    """A reported window end is believed only where a real window can end."""

    def test_the_far_future_end_that_froze_the_tool_is_estimated_instead(self) -> None:
        # Regression: a stream reporting resetsAt 253402300000 was recorded as a window
        # ending on 9999-12-31, so no tick ever pinged again.
        text = stream(rate_limit_line("allowed", 253402300000), SUCCESS_RESULT)
        outcome = trusted_window_end(parse_ping_output(text), _NOW)
        self.assertIsNone(outcome.resets_at)
        self.assertIn("9999-12-31", outcome.detail)
        self.assertIn("estimated instead", outcome.detail)
        # Regression: the text said "outside the next 5 hours", a bound that was
        # neither the one applied nor derived from the constants that set it.
        self.assertIn(
            "is outside (ping time, ping time + 5 h + 15 min]", outcome.detail
        )
        state = apply_outcome(_PRIOR, outcome, _NOW)
        self.assertEqual(
            (state.window_ends_at, state.window_end_source),
            (_NOW + WINDOW_LENGTH, SOURCE_ESTIMATED),
        )

    def test_the_bound_is_open_at_now_and_closed_at_its_far_end(self) -> None:
        latest = _NOW + WINDOW_LENGTH + WINDOW_END_SLACK
        rows = [
            ("in the past", _NOW - _HOUR, False),
            ("exactly now", _NOW, False),
            ("a second from now", _NOW + timedelta(seconds=1), True),
            ("a full window from now", _NOW + WINDOW_LENGTH, True),
            ("at the slack's end", latest, True),
            ("a second past it", latest + timedelta(seconds=1), False),
        ]
        for name, ends, kept in rows:
            for kind in (OUTCOME_OK, OUTCOME_LIMITED):
                with self.subTest(row=name, kind=kind):
                    outcome = trusted_window_end(PingOutcome(kind, ends, "d"), _NOW)
                    self.assertEqual(outcome.resets_at, ends if kept else None)
                    self.assertEqual(outcome.kind, kind)
                    self.assertEqual(outcome.detail == "d", kept)

    def test_an_outcome_with_no_end_is_unchanged(self) -> None:
        outcome = PingOutcome(OUTCOME_FAILED, None, "exit 1: boom")
        self.assertIs(trusted_window_end(outcome, _NOW), outcome)

    def test_the_slack_is_a_small_part_of_a_window(self) -> None:
        # Room for clock disagreement, not for a second window.
        self.assertGreater(WINDOW_END_SLACK, timedelta(0))
        self.assertLess(WINDOW_END_SLACK, _HOUR)


class DurationTextTest(unittest.TestCase):
    """A bound's length, as the log names it."""

    def test_hours_and_minutes_each_show_only_when_present(self) -> None:
        rows = [
            (timedelta(hours=5), "5 h"),
            (timedelta(minutes=15), "15 min"),
            (timedelta(hours=1, minutes=30), "1 h 30 min"),
            (timedelta(0), "0 min"),
        ]
        for delta, text in rows:
            with self.subTest(delta=delta):
                self.assertEqual(duration_text(delta), text)


class NextPingDueTest(unittest.TestCase):
    """The moment status reports as the next ping."""

    def test_next_ping_due(self) -> None:
        rows = [
            ("nothing recorded", None, None, None),
            ("start only", _NOW, None, _NOW),
            ("window only", None, _RESET, _RESET),
            ("both, the later wins", _NOW, _RESET, _RESET),
        ]
        for name, start, window_ends_at, expected in rows:
            with self.subTest(row=name):
                self.assertEqual(next_ping_due(start, window_ends_at), expected)


class ParseUserDatetimeTest(unittest.TestCase):
    """Operator-typed moments for --start and --last-run."""

    def test_parse_user_datetime_converts_an_offset_to_utc(self) -> None:
        self.assertEqual(parse_user_datetime("2026-01-01T07:00:00-05:00"), _NOW)
        self.assertEqual(parse_user_datetime("2026-01-01T12:00:00Z"), _NOW)

    def test_parse_user_datetime_reads_a_naive_moment_as_local_time(self) -> None:
        parsed = parse_user_datetime("2026-07-01T06:00")
        self.assertEqual(parsed.utcoffset(), timedelta(0))
        self.assertEqual(parsed, datetime(2026, 7, 1, 6, 0).astimezone(UTC))

    def test_parse_user_datetime_rejects_non_iso_text(self) -> None:
        # The last two parse, but converting their offset to UTC leaves datetime's range.
        for text in (
            "tomorrow morning",
            "0001-01-01T00:00+01:00",
            "9999-12-31T23:59-01:00",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_user_datetime(text)


if __name__ == "__main__":
    unittest.main()
