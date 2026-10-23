"""`--mode tick` end to end against fake seams: when it pings, and what it records."""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import sys
import tempfile
import unittest
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _cli_fakes import (  # noqa: E402 - import follows the sys.path bootstrap above
    FAKE_CLAUDE,
    FIXED_NOW,
    PRIVATE_DIR_MODE,
    install_config,
    make_seams,
)
from _fakes import (  # noqa: E402 - same bootstrap
    ERROR_RESULT,
    RESET_AT,
    RESET_EPOCH,
    SUCCESS_RESULT,
    RunResult,
    ScriptedRunner,
    rate_limit_line,
    stream,
)
from failure_alert import OSASCRIPT, UNSUPPORTED_ON_WIN32  # noqa: E402 - same bootstrap
from usage_window_primer import (  # noqa: E402 - same bootstrap
    EXIT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    LOG_DETAIL_MAX_CHARS,
    main,
)
from state_store import (  # noqa: E402 - same bootstrap
    LOG_FILENAME,
    STATE_FILENAME,
    PrimerConfig,
    PrimerState,
    load_state,
    save_config,
    save_state,
)

_HOUR = timedelta(hours=1)
_OK_STREAM = stream(rate_limit_line("allowed", RESET_EPOCH), SUCCESS_RESULT)
_OFFLINE: RunResult = (1, "", "offline")
_SHOWN: RunResult = (0, "", "")
_FAILING_TITLE = "Claude usage window primer is failing"
_RECOVERED_TITLE = "Claude usage window primer recovered"
# An expired-login-shaped error result whose raw JSON line holds the six characters of an
# escaped lone surrogate, which json.loads decodes into the character itself. The
# backslash is chr(92): an editing tool can decode a typed escape into the raw character.
_LONE_SURROGATE = chr(0xD800)
_SURROGATE_ERROR_RESULT = (
    '{"type": "result", "subtype": "success", "is_error": true, "result": "login '
    + chr(92)
    + 'ud800 expired"}'
)
_NO_PERMISSIONS = 0
# os.geteuid exists only off Windows, and the gate type-checks for win32 too, so it is
# read through getattr. Root reads a file whatever its mode bits say.
_GETEUID = getattr(os, "geteuid", None)
_RUNNING_AS_ROOT = _GETEUID is not None and _GETEUID() == 0


class TickTest(unittest.TestCase):
    """The tick table and the outcome table, through the CLI."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.state_dir = (self.home / "state").resolve()
        self.state_dir.mkdir(mode=PRIVATE_DIR_MODE)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _tick(
        self, answers: dict[tuple[str, str], list[tuple[int, str, str]]], *extra: str
    ) -> tuple[int, list[list[str]], str]:
        seams, runner, _out, err = make_seams(self.home, answers)
        code = main(
            ["--mode", "tick", "--state-dir", str(self.state_dir), *extra], seams=seams
        )
        return code, runner.commands(), err.getvalue()

    def test_tick_without_an_install_exits_2(self) -> None:
        code, commands, err = self._tick({})
        self.assertEqual((code, commands), (EXIT_USAGE, []))
        self.assertIn("run --mode install first", err)

    def test_tick_with_a_corrupt_state_exits_2_and_leaves_it(self) -> None:
        install_config(self.state_dir)
        (self.state_dir / STATE_FILENAME).write_bytes(b"{broken")
        code, commands, err = self._tick({})
        self.assertEqual((code, commands), (EXIT_USAGE, []))
        self.assertIn(STATE_FILENAME, err)
        self.assertEqual((self.state_dir / STATE_FILENAME).read_bytes(), b"{broken")

    def test_tick_before_start_does_nothing(self) -> None:
        install_config(self.state_dir, start=FIXED_NOW + _HOUR)
        code, commands, _err = self._tick({})
        self.assertEqual((code, commands), (EXIT_OK, []))
        self.assertFalse((self.state_dir / STATE_FILENAME).exists())
        self.assertFalse((self.state_dir / LOG_FILENAME).exists())

    def test_tick_inside_an_open_window_does_nothing(self) -> None:
        install_config(self.state_dir)
        saved = PrimerState(FIXED_NOW + _HOUR, "server", FIXED_NOW - _HOUR, "ok", 0)
        save_state(self.state_dir, saved)
        code, commands, _err = self._tick({})
        self.assertEqual((code, commands), (EXIT_OK, []))
        self.assertEqual(load_state(self.state_dir), saved)
        self.assertFalse((self.state_dir / LOG_FILENAME).exists())

    def test_tick_after_the_window_ends_pings_and_records_the_server_end(self) -> None:
        install_config(self.state_dir)
        save_state(
            self.state_dir,
            PrimerState(FIXED_NOW - _HOUR, "server", FIXED_NOW - 6 * _HOUR, "ok", 0),
        )
        code, commands, _err = self._tick({(FAKE_CLAUDE, "-p"): [(0, _OK_STREAM, "")]})
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][:2], [FAKE_CLAUDE, "-p"])
        self.assertEqual(
            load_state(self.state_dir),
            PrimerState(RESET_AT, "server", FIXED_NOW, "ok", 0, FIXED_NOW),
        )
        log = (self.state_dir / LOG_FILENAME).read_text(encoding="utf-8")
        self.assertIn(" ok window_ends_at=2026-01-01T18:00:00+00:00 ", log)

    def test_tick_with_no_recorded_window_pings(self) -> None:
        install_config(self.state_dir)
        code, commands, _err = self._tick({(FAKE_CLAUDE, "-p"): [(0, _OK_STREAM, "")]})
        self.assertEqual((code, len(commands)), (EXIT_OK, 1))

    def test_a_failed_ping_exits_1_and_keeps_the_window(self) -> None:
        install_config(self.state_dir)
        prior = PrimerState(FIXED_NOW - _HOUR, "server", FIXED_NOW - 6 * _HOUR, "ok", 2)
        save_state(self.state_dir, prior)
        code, _commands, _err = self._tick({(FAKE_CLAUDE, "-p"): [(1, "", "offline")]})
        self.assertEqual(code, EXIT_FAILED)
        state = load_state(self.state_dir)
        self.assertEqual(state.window_ends_at, prior.window_ends_at)
        self.assertEqual(
            (state.last_outcome, state.consecutive_failures), ("failed", 3)
        )
        self.assertIn(
            " failed ", (self.state_dir / LOG_FILENAME).read_text(encoding="utf-8")
        )

    def test_a_multi_line_failure_detail_writes_one_log_line(self) -> None:
        # A failed ping's detail is the CLI's stderr, which can span lines; the log holds
        # one line per ping attempt, and the cap trims by line.
        install_config(self.state_dir)
        answers = {(FAKE_CLAUDE, "-p"): [(1, "", "line one\nline two")]}
        code, _commands, _err = self._tick(answers)
        self.assertEqual(code, EXIT_FAILED)
        log = (self.state_dir / LOG_FILENAME).read_text(encoding="utf-8")
        lines = log.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("window_ends_at=none", lines[0])
        self.assertIn("line one line two", lines[0])

    def test_a_long_untrusted_detail_is_capped_in_the_log(self) -> None:
        # stderr is already capped by ping_runner, but a result's subtype repr comes from
        # the CLI's stream uncapped, so the log line bounds the detail itself.
        install_config(self.state_dir)
        long_result = json.dumps(
            {"type": "result", "subtype": "x" * 1000, "is_error": True}
        )
        answers = {(FAKE_CLAUDE, "-p"): [(0, stream(long_result), "")]}
        code, _commands, _err = self._tick(answers)
        self.assertEqual(code, EXIT_FAILED)
        log = (self.state_dir / LOG_FILENAME).read_text(encoding="utf-8")
        (line,) = log.splitlines()
        # The detail follows the moment, the kind and the window end.
        detail = line.split(" ", 3)[3]
        self.assertEqual(len(detail), LOG_DETAIL_MAX_CHARS)

    def test_terminal_escapes_in_the_detail_never_reach_the_log(self) -> None:
        # Regression: the CLI's own error text went into the log with its control
        # characters, so an OSC or CSI sequence in it acted on the terminal of whoever
        # read the log there. The escape and bell characters are built with chr().
        install_config(self.state_dir)
        escape, bell = chr(0x1B), chr(0x07)
        message = f"{escape}]0;owned{bell}login {escape}[31mexpired{escape}[0m"
        result = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "result": message,
            }
        )
        answers = {(FAKE_CLAUDE, "-p"): [(1, stream(result), "")]}
        code, _commands, _err = self._tick(answers)
        self.assertEqual(code, EXIT_FAILED)
        (line,) = (
            (self.state_dir / LOG_FILENAME).read_text(encoding="utf-8").splitlines()
        )
        self.assertTrue(line.isprintable(), repr(line))
        self.assertIn("]0;owned login [31mexpired [0m", line)

    def test_a_window_end_no_real_window_has_is_estimated_and_logged(self) -> None:
        # Regression: resetsAt 253402300000 was recorded as a window ending on
        # 9999-12-31, after which no tick ever pinged again.
        install_config(self.state_dir)
        frozen = stream(rate_limit_line("allowed", 253402300000), SUCCESS_RESULT)
        code, _commands, _err = self._tick({(FAKE_CLAUDE, "-p"): [(0, frozen, "")]})
        self.assertEqual(code, EXIT_OK)
        state = load_state(self.state_dir)
        self.assertEqual(
            (state.window_ends_at, state.window_end_source),
            (FIXED_NOW + timedelta(hours=5), "estimated"),
        )
        log = (self.state_dir / LOG_FILENAME).read_text(encoding="utf-8")
        self.assertIn("9999-12-31", log)
        self.assertIn("estimated instead", log)

    def test_a_recorded_batch_shim_is_refused_on_windows(self) -> None:
        # Regression: install refuses a claude.cmd now, but a config.json recorded
        # before that fix still named one, and every tick ran it through cmd.exe.
        shim = "C:\\fake\\npm\\claude.CMD"
        config = PrimerConfig(None, "haiku", "Reply with: ok", 5, shim)
        save_config(self.state_dir, config)
        seams, runner, _out, err = make_seams(
            self.home, {(shim, "-p"): [(0, _OK_STREAM, "")]}, platform="win32"
        )
        argv = ["--mode", "tick", "--state-dir", str(self.state_dir)]
        self.assertEqual(main(argv, seams=seams), EXIT_USAGE)
        self.assertEqual(runner.calls, [])
        self.assertIn("batch shim", err.getvalue())
        self.assertFalse((self.state_dir / STATE_FILENAME).exists())

    def test_a_recorded_batch_shim_still_runs_off_windows(self) -> None:
        # Only Windows starts a .cmd through cmd.exe; elsewhere the name is just a name.
        shim = "/opt/fake/bin/claude.cmd"
        config = PrimerConfig(None, "haiku", "Reply with: ok", 5, shim)
        save_config(self.state_dir, config)
        seams, runner, _out, err = make_seams(
            self.home, {(shim, "-p"): [(0, _OK_STREAM, "")]}
        )
        argv = ["--mode", "tick", "--state-dir", str(self.state_dir)]
        self.assertEqual(main(argv, seams=seams), EXIT_OK, err.getvalue())
        self.assertEqual(len(runner.calls), 1)

    def test_a_ping_output_file_needs_an_explicit_state_dir(self) -> None:
        # Regression: a hand-verification run without --state-dir classified the
        # recording into the live state directory, overwriting the real record.
        recorded = self.home / "recorded.jsonl"
        recorded.write_text(_OK_STREAM, encoding="utf-8", newline="\n")
        live = self.home / ".usage_window_primer"
        install_config(live)
        seams, runner, _out, err = make_seams(self.home, {})
        argv = ["--mode", "tick", "--ping-output-file", str(recorded)]
        self.assertEqual(main(argv, seams=seams), EXIT_USAGE)
        self.assertIn("--state-dir", err.getvalue())
        self.assertEqual(runner.calls, [])
        self.assertFalse((live / STATE_FILENAME).exists())
        self.assertFalse((live / LOG_FILENAME).exists())

    def test_a_limited_ping_records_the_reset(self) -> None:
        install_config(self.state_dir)
        limited = stream(rate_limit_line("rejected", RESET_EPOCH), ERROR_RESULT)
        code, _commands, _err = self._tick({(FAKE_CLAUDE, "-p"): [(1, limited, "")]})
        self.assertEqual(code, EXIT_OK)
        state = load_state(self.state_dir)
        self.assertEqual(
            (state.window_ends_at, state.last_outcome), (RESET_AT, "limited")
        )

    def test_ping_output_file_replaces_the_real_ping(self) -> None:
        install_config(self.state_dir)
        recorded = self.home / "recorded.jsonl"
        recorded.write_text(_OK_STREAM, encoding="utf-8", newline="\n")
        code, commands, _err = self._tick({}, "--ping-output-file", str(recorded))
        self.assertEqual((code, commands), (EXIT_OK, []))
        self.assertEqual(load_state(self.state_dir).window_ends_at, RESET_AT)

    def test_a_missing_ping_output_file_exits_2(self) -> None:
        install_config(self.state_dir)
        missing = str(self.home / "absent.jsonl")
        code, commands, err = self._tick({}, "--ping-output-file", missing)
        self.assertEqual((code, commands), (EXIT_USAGE, []))
        self.assertIn("--ping-output-file", err)
        # The OSError text already names the file; the message must not repeat it.
        self.assertEqual(err.count(missing), 1)

    @unittest.skipIf(_RUNNING_AS_ROOT, "root reads a file whatever its mode bits say")
    def test_an_unreadable_ping_output_file_exits_2(self) -> None:
        # Exit 1 means "a ping failed"; a file that exists but cannot be read is a bad
        # argument, refused before any decision or ping.
        install_config(self.state_dir)
        recorded = self.home / "unreadable.jsonl"
        recorded.write_text(_OK_STREAM, encoding="utf-8", newline="\n")
        recorded.chmod(_NO_PERMISSIONS)
        try:
            code, commands, err = self._tick({}, "--ping-output-file", str(recorded))
        finally:
            # Restored here rather than in a cleanup: cleanups run after tearDown has
            # already removed the directory.
            recorded.chmod(stat.S_IRUSR | stat.S_IWUSR)
        self.assertEqual((code, commands), (EXIT_USAGE, []))
        self.assertIn("--ping-output-file", err)
        self.assertFalse((self.state_dir / STATE_FILENAME).exists())
        self.assertFalse((self.state_dir / LOG_FILENAME).exists())


class TickAlertTest(unittest.TestCase):
    """A failure streak's start and end each send one notification through osascript."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.state_dir = (self.home / "state").resolve()
        self.state_dir.mkdir(mode=PRIVATE_DIR_MODE)
        install_config(self.state_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _save_failures(self, failures: int) -> None:
        # An ended window, so the tick pings.
        save_state(
            self.state_dir,
            PrimerState(FIXED_NOW - _HOUR, "server", None, "failed", failures),
        )

    def _tick(
        self, ping: RunResult, notify: RunResult = _SHOWN, platform: str = "darwin"
    ) -> tuple[int, list[list[str]], str]:
        answers = {(FAKE_CLAUDE, "-p"): [ping], (OSASCRIPT, "-e"): [notify]}
        seams, runner, _out, err = make_seams(self.home, answers, platform=platform)
        code = main(["--mode", "tick", "--state-dir", str(self.state_dir)], seams=seams)
        return code, runner.commands(), err.getvalue()

    def _notifications(self, commands: list[list[str]]) -> list[list[str]]:
        return [argv for argv in commands if argv[0] == OSASCRIPT]

    def test_the_failure_that_starts_a_streak_notifies_once(self) -> None:
        self._save_failures(11)
        code, commands, err = self._tick(_OFFLINE)
        self.assertEqual(code, EXIT_FAILED)
        (notification,) = self._notifications(commands)
        self.assertEqual(
            notification[-2:],
            [_FAILING_TITLE, "12 pings in a row have failed. Latest: exit 1: offline"],
        )
        self.assertEqual(err, "")

    def test_a_streak_on_windows_warns_instead_of_notifying(self) -> None:
        # No notification route on win32 yet. The tick still records and exits as
        # usual, and its WARNING line says why (task.err.log with no console).
        self._save_failures(11)
        code, commands, err = self._tick(_OFFLINE, platform="win32")
        self.assertEqual(code, EXIT_FAILED)
        self.assertEqual(self._notifications(commands), [])
        self.assertEqual(
            err,
            f"WARNING: could not show the alert notification: {UNSUPPORTED_ON_WIN32}\n",
        )

    def test_a_failure_inside_a_streak_does_not_notify(self) -> None:
        self._save_failures(12)
        code, commands, _err = self._tick(_OFFLINE)
        self.assertEqual(code, EXIT_FAILED)
        self.assertEqual(self._notifications(commands), [])

    def test_the_ping_that_ends_a_streak_notifies_recovery(self) -> None:
        self._save_failures(12)
        code, commands, _err = self._tick((0, _OK_STREAM, ""))
        self.assertEqual(code, EXIT_OK)
        (notification,) = self._notifications(commands)
        self.assertEqual(
            notification[-2:],
            [_RECOVERED_TITLE, "A ping got through after 12 failures in a row."],
        )

    def test_a_lone_surrogate_in_the_cli_error_still_logs_and_notifies(self) -> None:
        # The detail quotes the CLI's own result text. A lone surrogate there must not
        # crash the tick after its state is saved: that would lose the log line and the
        # streak's only failing notification.
        self._save_failures(11)
        ping: RunResult = (1, stream(_SURROGATE_ERROR_RESULT), "")
        code, commands, _err = self._tick(ping)
        self.assertEqual(code, EXIT_FAILED)
        log = (self.state_dir / LOG_FILENAME).read_text(encoding="utf-8")
        self.assertEqual(len(log.splitlines()), 1)
        (notification,) = self._notifications(commands)
        self.assertNotIn(_LONE_SURROGATE, notification[-1])

    def _recorded(self) -> tuple[bytes, bytes]:
        return (
            (self.state_dir / STATE_FILENAME).read_bytes(),
            (self.state_dir / LOG_FILENAME).read_bytes(),
        )

    def test_a_refused_notification_changes_nothing_but_a_warning(self) -> None:
        # The live agent must keep its exit code, state and log whatever osascript does:
        # a refusal, or a missing osascript, which the runner reports as exit 127.
        self._save_failures(11)
        shown_code, _commands, shown_err = self._tick(_OFFLINE)
        shown = self._recorded()
        self.assertEqual((shown_code, shown_err), (EXIT_FAILED, ""))
        for refusal, reported in (
            ((1, "", "boom"), "exit 1: boom"),
            ((127, "", "not found"), "exit 127: not found"),
        ):
            with self.subTest(refusal=refusal):
                self._save_failures(11)
                (self.state_dir / LOG_FILENAME).unlink()
                code, _commands, err = self._tick(_OFFLINE, refusal)
                self.assertEqual((code, self._recorded()), (shown_code, shown))
                self.assertEqual(
                    err, f"WARNING: could not show the alert notification: {reported}\n"
                )

    def test_the_tick_records_before_it_notifies(self) -> None:
        # validate -> act -> record -> alert: a notification that hangs until its
        # timeout, or kills the process, must never cost the record.
        self._save_failures(11)
        scripted = ScriptedRunner(
            {(FAKE_CLAUDE, "-p"): [_OFFLINE], (OSASCRIPT, "-e"): [_SHOWN]}
        )
        seen: list[tuple[int, bool]] = []

        def runner(
            argv: list[str],
            timeout: float,
            env: Mapping[str, str] | None,
            cwd: Path | None,
        ) -> RunResult:
            if argv[0] == OSASCRIPT:
                seen.append(
                    (
                        load_state(self.state_dir).consecutive_failures,
                        (self.state_dir / LOG_FILENAME).exists(),
                    )
                )
            return scripted(argv, timeout, env, cwd)

        seams, _runner, _out, _err = make_seams(self.home, {})
        main(
            ["--mode", "tick", "--state-dir", str(self.state_dir)],
            seams=dataclasses.replace(seams, run=runner),
        )
        self.assertEqual(seen, [(12, True)])


if __name__ == "__main__":
    unittest.main()
