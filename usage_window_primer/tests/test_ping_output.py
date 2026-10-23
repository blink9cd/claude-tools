"""Classifying a headless ping's stream-json output, one measured shape per case."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TOOL_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import (  # noqa: E402 - import follows the sys.path bootstrap above
    CLI_ERROR_RESULT,
    ERROR_RESULT,
    RESET_AT,
    RESET_EPOCH,
    SUCCESS_RESULT,
    SYSTEM_INIT,
    rate_limit_line,
    stream,
)
from ping_output import (  # noqa: E402 - same bootstrap
    NO_RESULT_DETAIL,
    OUTCOME_FAILED,
    OUTCOME_LIMITED,
    OUTCOME_OK,
    describe_last_event,
    parse_ping_output,
)


def _bare_rate_limit(rate_limit_type: str) -> str:
    return json.dumps(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed",
                "resetsAt": RESET_EPOCH,
                "rateLimitType": rate_limit_type,
            },
        }
    )


class ParsePingOutputTest(unittest.TestCase):
    """Every outcome a stream can classify as, plus the malformed-input family."""

    def test_success_with_a_rate_limit_event_reports_the_window_end(self) -> None:
        text = stream(
            SYSTEM_INIT, rate_limit_line("allowed", RESET_EPOCH), SUCCESS_RESULT
        )
        outcome = parse_ping_output(text)
        self.assertEqual((outcome.kind, outcome.resets_at), (OUTCOME_OK, RESET_AT))

    def test_success_without_a_rate_limit_event_reports_no_window_end(self) -> None:
        outcome = parse_ping_output(stream(SYSTEM_INIT, SUCCESS_RESULT))
        self.assertEqual((outcome.kind, outcome.resets_at), (OUTCOME_OK, None))

    def test_rejected_status_is_limited_even_beside_an_error_result(self) -> None:
        outcome = parse_ping_output(
            stream(rate_limit_line("rejected", RESET_EPOCH), ERROR_RESULT)
        )
        self.assertEqual((outcome.kind, outcome.resets_at), (OUTCOME_LIMITED, RESET_AT))

    def test_near_limit_warning_classifies_by_the_result(self) -> None:
        text = stream(rate_limit_line("allowed_warning", RESET_EPOCH), SUCCESS_RESULT)
        outcome = parse_ping_output(text)
        self.assertEqual((outcome.kind, outcome.resets_at), (OUTCOME_OK, RESET_AT))

    def test_rejected_without_a_window_end_falls_back_to_the_result(self) -> None:
        outcome = parse_ping_output(
            stream(rate_limit_line("rejected", None), ERROR_RESULT)
        )
        self.assertEqual(outcome.kind, OUTCOME_FAILED)

    def test_no_result_event_is_failed(self) -> None:
        outcome = parse_ping_output(
            stream(SYSTEM_INIT, rate_limit_line("allowed", RESET_EPOCH))
        )
        self.assertEqual(outcome.kind, OUTCOME_FAILED)
        self.assertEqual(outcome.detail, NO_RESULT_DETAIL)
        self.assertEqual(NO_RESULT_DETAIL, "no result event in the ping output")

    def test_error_result_is_failed(self) -> None:
        # No message beside another subtype: the detail is the subtype alone, unchanged.
        outcome = parse_ping_output(stream(ERROR_RESULT))
        self.assertEqual(outcome.kind, OUTCOME_FAILED)
        self.assertEqual(outcome.detail, "result subtype 'error_during_execution'")

    def test_success_subtype_with_is_error_true_is_failed(self) -> None:
        line = json.dumps({"type": "result", "subtype": "success", "is_error": True})
        self.assertEqual(parse_ping_output(stream(line)).kind, OUTCOME_FAILED)

    def test_error_result_with_success_subtype_names_the_cli_message(self) -> None:
        outcome = parse_ping_output(stream(SYSTEM_INIT, CLI_ERROR_RESULT))
        self.assertEqual(outcome.kind, OUTCOME_FAILED)
        self.assertEqual(outcome.detail, "CLI error: OAuth session expired")

    def test_error_result_with_success_subtype_and_no_message(self) -> None:
        event = {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": "  ",
        }
        outcome = parse_ping_output(stream(json.dumps(event)))
        self.assertEqual(outcome.detail, "CLI error result with no message")

    def test_other_subtype_keeps_its_name_and_adds_the_message(self) -> None:
        event = {
            "type": "result",
            "subtype": "error_max_turns",
            "is_error": True,
            "result": "stopped",
        }
        outcome = parse_ping_output(stream(json.dumps(event)))
        self.assertEqual(outcome.detail, "result subtype 'error_max_turns': stopped")

    def test_non_string_message_counts_as_absent(self) -> None:
        event = {"type": "result", "subtype": "success", "is_error": True, "result": 7}
        outcome = parse_ping_output(stream(json.dumps(event)))
        self.assertEqual(outcome.detail, "CLI error result with no message")

    def test_top_level_resets_at_is_used_when_unified_windows_is_absent(self) -> None:
        outcome = parse_ping_output(
            stream(_bare_rate_limit("five_hour"), SUCCESS_RESULT)
        )
        self.assertEqual(outcome.resets_at, RESET_AT)

    def test_top_level_resets_at_of_another_window_is_ignored(self) -> None:
        outcome = parse_ping_output(
            stream(_bare_rate_limit("seven_day"), SUCCESS_RESULT)
        )
        self.assertEqual((outcome.kind, outcome.resets_at), (OUTCOME_OK, None))

    def test_lines_that_are_not_json_objects_are_skipped(self) -> None:
        text = stream(
            "not json",
            "[1, 2]",
            "",
            rate_limit_line("allowed", RESET_EPOCH),
            SUCCESS_RESULT,
        )
        outcome = parse_ping_output(text)
        self.assertEqual((outcome.kind, outcome.resets_at), (OUTCOME_OK, RESET_AT))

    def test_unrepresentable_window_ends_read_as_unknown(self) -> None:
        # JSON true is an int subclass in Python and must not read as 1970; a string is
        # not an epoch; infinity and a 400-digit integer overflow datetime.
        for value in (True, str(RESET_EPOCH), float("inf"), 10**400):
            with self.subTest(value=repr(value)[:20]):
                text = stream(rate_limit_line("allowed", value), SUCCESS_RESULT)
                outcome = parse_ping_output(text)
                self.assertEqual((outcome.kind, outcome.resets_at), (OUTCOME_OK, None))

    def test_the_last_rate_limit_event_wins(self) -> None:
        text = stream(
            rate_limit_line("allowed", RESET_EPOCH - 3600),
            rate_limit_line("allowed", RESET_EPOCH),
            SUCCESS_RESULT,
        )
        self.assertEqual(parse_ping_output(text).resets_at, RESET_AT)

    def test_a_non_object_rate_limit_info_carries_nothing(self) -> None:
        line = json.dumps({"type": "rate_limit_event", "rate_limit_info": "five_hour"})
        outcome = parse_ping_output(stream(line, SUCCESS_RESULT))
        self.assertEqual((outcome.kind, outcome.resets_at), (OUTCOME_OK, None))


class DescribeLastEventTest(unittest.TestCase):
    """How far a ping that never finished got, named by the last event it wrote."""

    def test_no_text_is_no_output(self) -> None:
        for text in ("", " \n\t"):
            with self.subTest(text=text):
                self.assertEqual(describe_last_event(text), "no output")

    def test_text_with_no_json_object_is_no_complete_event(self) -> None:
        # A child killed mid-write leaves a torn line; a JSON array is not an event.
        for text in ("{partial", "[1, 2]\n"):
            with self.subTest(text=text):
                self.assertEqual(describe_last_event(text), "no complete event")

    def test_type_and_subtype_name_the_event(self) -> None:
        self.assertEqual(describe_last_event(stream(SYSTEM_INIT)), "system/init")

    def test_the_last_event_wins(self) -> None:
        text = stream(SYSTEM_INIT, json.dumps({"type": "assistant"}))
        self.assertEqual(describe_last_event(text), "assistant")

    def test_a_torn_last_line_leaves_the_event_before_it(self) -> None:
        # The shape a kill leaves: whole events, then the one it interrupted.
        text = stream(SYSTEM_INIT) + '{"type": "assi'
        self.assertEqual(describe_last_event(text), "system/init")

    def test_a_non_string_type_reads_as_unknown(self) -> None:
        text = stream(json.dumps({"type": 5, "subtype": "x"}))
        self.assertEqual(describe_last_event(text), "?/x")

    def test_a_non_string_subtype_is_left_out(self) -> None:
        text = stream(json.dumps({"type": "result", "subtype": 3}))
        self.assertEqual(describe_last_event(text), "result")

    def test_an_empty_label_reads_as_unknown(self) -> None:
        text = stream(json.dumps({"type": ""}))
        self.assertEqual(describe_last_event(text), "?")

    def test_a_long_label_is_cut(self) -> None:
        text = stream(json.dumps({"type": "t" * 300}))
        self.assertEqual(describe_last_event(text), "t" * 60)


if __name__ == "__main__":
    unittest.main()
