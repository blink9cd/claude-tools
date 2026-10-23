"""`--mode status`, the parser, and `main` with its real default seams."""

from __future__ import annotations

import contextlib
import io
import re
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _cli_fakes import FIXED_NOW, install_config, make_seams  # noqa: E402 - import follows the sys.path bootstrap above
from _fakes import RunResult  # noqa: E402 - same bootstrap
from usage_window_primer import (  # noqa: E402 - same bootstrap
    EXIT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    Seams,
    build_parser,
    main,
)
from launch_agent import ID_COMMAND, LAUNCHCTL  # noqa: E402 - same bootstrap
from state_store import PrimerState, save_state  # noqa: E402 - same bootstrap
from status_view import format_local  # noqa: E402 - same bootstrap

_HOUR = timedelta(hours=1)
_MINUTE = timedelta(minutes=1)
_START = FIXED_NOW - 6 * _HOUR
_ENDS = FIXED_NOW + 3 * _HOUR
_LAST = FIXED_NOW - 2 * _HOUR
_ATTEMPT = FIXED_NOW - 5 * _MINUTE
_SCHEDULER_ROW = "scheduler (launchd agent)"
# A label ends at the first run of two or more spaces; no label or value holds one.
_COLUMN_GAP = re.compile(r" {2,}")


def _answers(
    print_result: RunResult, id_result: RunResult = (0, "501\n", "")
) -> dict[tuple[str, str], list[RunResult]]:
    return {(ID_COMMAND, "-u"): [id_result], (LAUNCHCTL, "print"): [print_result]}


class StatusTest(unittest.TestCase):
    """Every recorded fact, and the agent's loaded state."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.state_dir = (self.home / "state").resolve()
        self.state_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _status(
        self, answers: dict[tuple[str, str], list[RunResult]]
    ) -> tuple[int, dict[str, str], str]:
        seams, _runner, out, err = make_seams(self.home, answers)
        code = main(
            ["--mode", "status", "--state-dir", str(self.state_dir)], seams=seams
        )
        lines = out.getvalue().splitlines()
        # Every value starts in one column, wide enough for the longest label.
        columns = {len(line) - len(_COLUMN_GAP.split(line, 1)[1]) for line in lines}
        self.assertLessEqual(len(columns), 1, out.getvalue())
        rows = dict(_COLUMN_GAP.split(line, 1) for line in lines)
        return code, rows, err.getvalue()

    def test_status_prints_every_recorded_fact_in_order(self) -> None:
        install_config(self.state_dir, start=_START)
        save_state(
            self.state_dir,
            PrimerState(_ENDS, "server", _LAST, "failed", 2, _ATTEMPT),
        )
        code, rows, _err = self._status(_answers((0, "", "")))
        self.assertEqual(code, EXIT_OK)
        # A list, not a dict comparison: the order the rows print in is part of the
        # contract usage_window_primer.md describes.
        self.assertEqual(
            list(rows.items()),
            [
                ("start", format_local(_START)),
                ("last attempt", f"{format_local(_ATTEMPT)} (failed)"),
                ("last success", format_local(_LAST)),
                ("window ends", f"{format_local(_ENDS)} (server)"),
                ("next ping due", format_local(_ENDS)),
                ("consecutive failures", "2"),
                (_SCHEDULER_ROW, "loaded"),
                ("health", "ok"),
            ],
        )

    def test_status_is_healthy_below_the_failure_threshold(self) -> None:
        install_config(self.state_dir)
        save_state(
            self.state_dir,
            PrimerState(_ENDS, "server", _LAST, "failed", 11, _ATTEMPT),
        )
        code, rows, _err = self._status(_answers((0, "", "")))
        self.assertEqual((code, rows["health"]), (EXIT_OK, "ok"))

    def test_status_exits_1_while_a_failure_streak_lasts(self) -> None:
        # Nothing else reads the agent's log, state or exit code, so a streak must show
        # in the one command a person or a monitor runs to ask whether it is working.
        install_config(self.state_dir)
        save_state(
            self.state_dir,
            PrimerState(_ENDS, "server", _LAST, "failed", 12, _ATTEMPT),
        )
        code, rows, _err = self._status(_answers((0, "", "")))
        self.assertEqual(
            (code, rows["health"]), (EXIT_FAILED, "failing (12 pings in a row)")
        )

    def test_status_dates_a_failure_by_its_attempt_not_the_last_success(self) -> None:
        # The measured defect: after days of failed ticks, status paired the last
        # SUCCESS's time with the latest outcome, dating the failure days too early.
        install_config(self.state_dir)
        save_state(
            self.state_dir,
            PrimerState(_ENDS, "server", _LAST, "failed", 674, _ATTEMPT),
        )
        _code, rows, _err = self._status(_answers((0, "", "")))
        self.assertEqual(
            (rows["last attempt"], rows["last success"]),
            (f"{format_local(_ATTEMPT)} (failed)", format_local(_LAST)),
        )

    def test_status_reads_a_state_written_before_attempts_were_recorded(self) -> None:
        # A state.json from before last_attempt_at existed recorded only successful
        # pings: its last_ping_at is the last attempt unless the last outcome failed.
        install_config(self.state_dir)
        cases: dict[str | None, str] = {
            "ok": f"{format_local(_LAST)} (ok)",
            "limited": f"{format_local(_LAST)} (limited)",
            "failed": "unknown (failed)",
            # No outcome beside a ping time is an install's --last-run: not an attempt.
            None: "never (none)",
        }
        for outcome, expected in cases.items():
            with self.subTest(outcome=outcome):
                # A --last-run install records its window end as `manual`.
                source = "server" if outcome is not None else "manual"
                save_state(
                    self.state_dir, PrimerState(_ENDS, source, _LAST, outcome, 0)
                )
                _code, rows, _err = self._status(_answers((0, "", "")))
                self.assertEqual(
                    (rows["last attempt"], rows["last success"]),
                    (expected, format_local(_LAST)),
                )

    def test_status_shows_the_next_tick_when_the_ping_is_already_due(self) -> None:
        install_config(self.state_dir)
        _code, rows, _err = self._status(_answers((0, "", "")))
        self.assertEqual(rows["next ping due"], "at the next tick")
        self.assertEqual(rows["start"], "not set")
        self.assertEqual(rows["last attempt"], "never (none)")
        self.assertEqual(rows["last success"], "never")
        self.assertEqual(rows["window ends"], "unknown (none)")

    def test_status_reports_an_agent_that_is_not_loaded(self) -> None:
        install_config(self.state_dir)
        _code, rows, _err = self._status(_answers((113, "", "Could not find service")))
        self.assertEqual(rows[_SCHEDULER_ROW], "not loaded")

    def test_status_reports_an_unknown_agent_when_id_fails(self) -> None:
        install_config(self.state_dir)
        _code, rows, _err = self._status(
            _answers((0, "", ""), id_result=(1, "", "id: failed"))
        )
        self.assertTrue(rows[_SCHEDULER_ROW].startswith("unknown ("))

    def test_status_reports_an_unknown_agent_when_print_is_unreadable(self) -> None:
        install_config(self.state_dir)
        _code, rows, _err = self._status(_answers((124, "", "timed out")))
        self.assertTrue(rows[_SCHEDULER_ROW].startswith("unknown ("))

    def test_status_without_an_install_exits_2(self) -> None:
        code, rows, err = self._status({})
        self.assertEqual((code, rows), (EXIT_USAGE, {}))
        self.assertIn("run --mode install first", err)


class ParserAndMainTest(unittest.TestCase):
    """The parser's contract and the real default seams."""

    def test_main_builds_real_seams_when_none_are_given(self) -> None:
        # A status call with no install stops before any command runs, so the real
        # seams are constructed and exercised without touching launchd or the network.
        with (
            tempfile.TemporaryDirectory() as tmp,
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            code = main(["--mode", "status", "--state-dir", tmp])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("run --mode install first", err.getvalue())

    def test_default_seams_read_the_real_clock_and_path(self) -> None:
        # Covers the two default callables no other test reaches: every other test
        # injects its own clock and PATH lookup.
        seams = Seams()
        self.assertLess(abs(seams.now() - datetime.now(UTC)), timedelta(seconds=5))
        self.assertIsNone(seams.which("usage_window_primer-no-such-command"))

    def test_help_shows_defaults_and_prerequisites(self) -> None:
        text = build_parser(Path("/opt/fake/state")).format_help()
        for fragment in (
            "(default: haiku)",
            "(default: 5)",
            "(default: /opt/fake/state)",
            "Prerequisites: macOS (a launchd user agent) or Windows (a Task Scheduler "
            "task)",
            "Exit codes:",
            "the scheduler did not confirm the job",
            "--mode status found a failure streak",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, " ".join(text.split()))

    def test_mode_is_required(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            build_parser(Path("/opt/fake/state")).parse_args([])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
