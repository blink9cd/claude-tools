"""The failure-streak alert: when it fires, what it says, and how it reaches osascript.

Everything here but three tests is pure or runs against a `ScriptedRunner`. The argv
route tests run the real `osascript`, with a script that only RETURNS its arguments, so
they show nothing on screen and change nothing on the host while proving that text
passed as argv arrives unaltered -- the quoting layer the notification route relies on
not having -- and that a dash-leading title is not taken for an option. They call
osascript by its absolute path, as the tool does, which a stand-in first on PATH cannot
answer, so `USAGE_WINDOW_PRIMER_OFFLINE=1` skips them for a run that must not reach the real one,
as in an export's proof. The compile
check runs the real `osacompile` on the shipped notification script into a temporary
directory: it compiles the script without running it, so a typo is
caught on every run, not only by the opt-in live test, and nothing is shown.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TOOL_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import Call, ScriptedRunner  # noqa: E402 - import follows the sys.path bootstrap above
from failure_alert import (  # noqa: E402 - same bootstrap
    NOTIFY_TIMEOUT_SECONDS,
    OSASCRIPT,
    UNSUPPORTED_ON_WIN32,
    Alert,
    alert_for,
    build_notify_command,
    is_failing,
    send_alert,
)
from process_run import run_process  # noqa: E402 - same bootstrap

_FAILING_TITLE = "Claude usage window primer is failing"
_RECOVERED_TITLE = "Claude usage window primer recovered"
_MESSAGE_MAX_CHARS = 200
_LONG_DETAIL_CHARS = 1000
# Every character a quoting layer could mangle: a leading dash (an option to a parser
# that has not stopped), double quotes (AppleScript's string delimiter) and backslashes
# (its escape character).
_HOSTILE_TEXT = '-starts "quoted" \\ back\\slash'
# Built with chr() rather than typed as escapes: an editing tool can decode a typed escape
# into the raw character, and a raw lone surrogate cannot even be saved as UTF-8.
_NUL = chr(0)
_LONE_SURROGATE = chr(0xD800)
_OSASCRIPT_PATH = shutil.which(OSASCRIPT)
#: Set to 1 to skip the live argv tests on a run that must not reach the real osascript:
#: an export's proof sets it, because the absolute path the tests call escapes the
#: stand-in it puts first on PATH. Read here rather than inferred, so the skip is the
#: suite's own rule.
_OFFLINE_ENV = "USAGE_WINDOW_PRIMER_OFFLINE"
_OSACOMPILE = "osacompile"
_OSACOMPILE_PATH = shutil.which(_OSACOMPILE)
_DISPLAY_LINE = "display notification (item 2 of argv) with title (item 1 of argv)"
# Generous for a compile that takes well under a second: the suite may run on a loaded host.
_COMPILE_TIMEOUT_SECONDS = 30.0


class AlertForTest(unittest.TestCase):
    """One alert as a streak starts and one as it clears, and nothing in between.

    The counts are literal, not derived from the threshold constant, so a changed
    threshold fails here instead of moving every expectation along with it.
    """

    def test_the_failure_that_reaches_the_threshold_alerts(self) -> None:
        self.assertEqual(
            alert_for(11, 12, "d"),
            Alert(_FAILING_TITLE, "12 pings in a row have failed. Latest: d"),
        )

    def test_a_failure_past_the_threshold_does_not_alert_again(self) -> None:
        self.assertIsNone(alert_for(12, 13, "d"))

    def test_a_ping_that_ends_a_streak_alerts_recovery(self) -> None:
        self.assertEqual(
            alert_for(12, 0, ""),
            Alert(_RECOVERED_TITLE, "A ping got through after 12 failures in a row."),
        )

    def test_a_ping_that_ends_a_short_run_of_failures_is_silent(self) -> None:
        self.assertIsNone(alert_for(11, 0, ""))

    def test_a_first_failure_is_silent(self) -> None:
        self.assertIsNone(alert_for(0, 1, "d"))

    def test_a_long_detail_is_cut_to_the_message_cap(self) -> None:
        alert = alert_for(11, 12, "x" * _LONG_DETAIL_CHARS)
        assert alert is not None
        self.assertEqual(len(alert.message), _MESSAGE_MAX_CHARS)

    def test_a_multi_line_detail_collapses_to_one_line(self) -> None:
        # The CLI's own error message can span lines; a notification shows one.
        alert = alert_for(11, 12, "line one\n\n  line two")
        assert alert is not None
        self.assertTrue(alert.message.endswith("Latest: line one line two"))

    def test_whitespace_collapses_before_the_cut(self) -> None:
        # Cut first, a run of blank lines would spend the cap and drop the text after it.
        alert = alert_for(11, 12, "first" + "\n" * _LONG_DETAIL_CHARS + "last")
        assert alert is not None
        self.assertTrue(alert.message.endswith("Latest: first last"))

    def test_a_nul_in_the_detail_never_reaches_the_message(self) -> None:
        # Popen refuses an argument holding a NUL, so the notification would never run.
        alert = alert_for(11, 12, f"a{_NUL}b")
        assert alert is not None
        self.assertNotIn(_NUL, alert.message)

    def test_a_lone_surrogate_in_the_detail_never_reaches_the_message(self) -> None:
        # A JSON escape can decode to a lone surrogate, which no argv can encode.
        alert = alert_for(11, 12, f"a{_LONE_SURROGATE}b")
        assert alert is not None
        self.assertNotIn(_LONE_SURROGATE, alert.message)


class IsFailingTest(unittest.TestCase):
    """The streak status reports is the same threshold the alert fires at."""

    def test_the_threshold_is_where_failing_starts(self) -> None:
        self.assertFalse(is_failing(11))
        self.assertTrue(is_failing(12))


class NotifyCommandTest(unittest.TestCase):
    """The text travels as argv, never inside the AppleScript source."""

    def test_the_command_passes_title_then_message_as_arguments(self) -> None:
        self.assertEqual(
            build_notify_command(Alert("T", "M")),
            [
                "/usr/bin/osascript",
                "-e",
                "on run argv",
                "-e",
                "display notification (item 2 of argv) with title (item 1 of argv)",
                "-e",
                "end run",
                "--",
                "T",
                "M",
            ],
        )

    def test_a_separator_ends_option_parsing_just_before_the_title(self) -> None:
        # Regression: without `--`, a title starting with `-` is read as an osascript
        # option -- a usage error, exit 2, and no notification.
        command = build_notify_command(Alert("-T", "-M"))
        self.assertEqual(command[-3:], ["--", "-T", "-M"])
        self.assertEqual(command.count("--"), 1)


class SendAlertTest(unittest.TestCase):
    """What a notification run reports back, through a scripted runner."""

    def test_a_shown_notification_reports_nothing(self) -> None:
        alert = Alert("T", "M")
        runner = ScriptedRunner({(OSASCRIPT, "-e"): [(0, "", "")]})
        self.assertIsNone(send_alert(runner, alert, platform="darwin"))
        self.assertEqual(
            runner.calls,
            [Call(build_notify_command(alert), NOTIFY_TIMEOUT_SECONDS, None, None)],
        )

    def test_a_refused_notification_reports_the_exit_and_error(self) -> None:
        runner = ScriptedRunner({(OSASCRIPT, "-e"): [(1, "", "execution error\n")]})
        self.assertEqual(
            send_alert(runner, Alert("T", "M"), platform="darwin"),
            "exit 1: execution error",
        )

    def test_win32_reports_unsupported_and_runs_nothing(self) -> None:
        # No notification route on Windows yet, so nothing is spawned and the
        # caller's usual WARNING line carries the reason.
        runner = ScriptedRunner({})
        self.assertEqual(
            send_alert(runner, Alert("T", "M"), platform="win32"), UNSUPPORTED_ON_WIN32
        )
        self.assertEqual(runner.calls, [])


def _argv_route_skip_reason(
    environ: Mapping[str, str], osascript: str | None
) -> str | None:
    """Why the live argv tests must not run under *environ*, else None.

    *osascript* is where a PATH lookup found it, or None when it found nothing.
    """
    if environ.get(_OFFLINE_ENV) == "1":
        return f"{_OFFLINE_ENV}=1 - live osascript argv tests skipped"
    if osascript is None:
        return "needs osascript on PATH"
    return None


_ARGV_ROUTE_SKIP = _argv_route_skip_reason(os.environ, _OSASCRIPT_PATH)


class ArgvRouteSkipTest(unittest.TestCase):
    """When the live argv tests below run, and why they are skipped otherwise."""

    def test_the_offline_switch_skips_them_even_with_osascript_present(self) -> None:
        self.assertEqual(
            _argv_route_skip_reason(
                {"USAGE_WINDOW_PRIMER_OFFLINE": "1"}, "/bin/osascript"
            ),
            "USAGE_WINDOW_PRIMER_OFFLINE=1 - live osascript argv tests skipped",
        )

    def test_a_missing_osascript_skips_them(self) -> None:
        self.assertEqual(_argv_route_skip_reason({}, None), "needs osascript on PATH")

    def test_they_run_with_osascript_present_and_the_switch_unset(self) -> None:
        self.assertIsNone(
            _argv_route_skip_reason(
                {"USAGE_WINDOW_PRIMER_OFFLINE": "0"}, "/bin/osascript"
            )
        )


@unittest.skipIf(_ARGV_ROUTE_SKIP is not None, _ARGV_ROUTE_SKIP or "")
class ArgvRouteTest(unittest.TestCase):
    """Text given to osascript as argv reaches the script byte for byte."""

    def test_hostile_text_passes_through_argv_unaltered(self) -> None:
        # The same route the notification takes, with RETURN in place of display
        # notification: the argument comes back on stdout instead of on screen.
        argv = [
            OSASCRIPT,
            "-e",
            "on run argv",
            "-e",
            "return item 2 of argv",
            "-e",
            "end run",
            "title",
            _HOSTILE_TEXT,
        ]
        code, out, err = run_process(argv, NOTIFY_TIMEOUT_SECONDS, None, None)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.removesuffix("\n"), _HOSTILE_TEXT)

    def test_a_dash_leading_title_reaches_the_script_as_an_argument(self) -> None:
        # The shipped command itself, with its display line swapped for one that RETURNS
        # both arguments: everything osascript parses -- the options, any separator, the
        # argv -- is the real command's, and nothing is shown on screen.
        command = build_notify_command(Alert(_HOSTILE_TEXT, _HOSTILE_TEXT))
        display_line = command.index(_DISPLAY_LINE)
        command[display_line] = 'return (item 1 of argv) & "|" & (item 2 of argv)'
        code, out, err = run_process(command, NOTIFY_TIMEOUT_SECONDS, None, None)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.removesuffix("\n"), f"{_HOSTILE_TEXT}|{_HOSTILE_TEXT}")


@unittest.skipUnless(_OSACOMPILE_PATH, "needs osacompile on PATH")
class NotifyScriptCompilesTest(unittest.TestCase):
    """The shipped notification script compiles; compiling it shows nothing."""

    def test_the_notification_script_compiles(self) -> None:
        # Otherwise only the opt-in live test would meet a typo in the script, and the
        # one push channel would stay broken in silence. The script lines are sliced out
        # of the real command, between `osascript` and the `--` before the arguments.
        command = build_notify_command(Alert("T", "M"))
        script_options = command[1 : command.index("--")]
        self.assertTrue(all(option == "-e" for option in script_options[::2]))
        with tempfile.TemporaryDirectory() as scratch:
            compiled = Path(scratch) / "notify.scpt"
            argv = [_OSACOMPILE, "-o", str(compiled), *script_options]
            code, _out, err = run_process(argv, _COMPILE_TIMEOUT_SECONDS, None, None)
        self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
