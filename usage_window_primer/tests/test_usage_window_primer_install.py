"""`--mode install` and `--mode uninstall` end to end against fake seams."""

from __future__ import annotations

import contextlib
import dataclasses
import os
import plistlib
import stat
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _cli_fakes import FIXED_NOW, make_seams  # noqa: E402 - import follows the sys.path bootstrap above
from _fakes import RunResult  # noqa: E402 - same bootstrap
from usage_window_primer import EXIT_FAILED, EXIT_OK, EXIT_USAGE, main  # noqa: E402 - same bootstrap
from launch_agent import ID_COMMAND, LAUNCHCTL, LAUNCHD_LABEL  # noqa: E402 - same bootstrap
from state_store import (  # noqa: E402 - same bootstrap
    CONFIG_FILENAME,
    STATE_FILENAME,
    PrimerConfig,
    load_config,
    load_state,
    to_utc_iso,
)
from task_agent import schtasks_path  # noqa: E402 - same bootstrap

# The environment WindowsLifecycleTest injects names no SYSTEMROOT, so the fallback.
_SCHTASKS = schtasks_path({})

_OK: RunResult = (0, "", "")
_NOT_FOUND: RunResult = (113, "", "Could not find service")
# Stands for "the real CLI file setUp writes": install stats the path `which` returns, so
# the default lookup must name a file that exists and has content.
_REAL_CLAUDE = "<the real claude file>"
# Built with chr(): an editing tool can decode a typed escape into the raw character.
_ESC = chr(0x1B)
_POSIX_HOST = os.name == "posix"
_NEEDS_POSIX = "needs POSIX mode bits; the state-dir guard skips a non-POSIX host"


def _launchd_answers(
    **overrides: list[RunResult],
) -> dict[tuple[str, str], list[RunResult]]:
    # A fresh install: the pre-bootstrap read finds no agent, every later read finds it.
    answers: dict[tuple[str, str], list[RunResult]] = {
        (ID_COMMAND, "-u"): [(0, "501\n", "")],
        (LAUNCHCTL, "bootout"): [_OK],
        (LAUNCHCTL, "bootstrap"): [_OK],
        (LAUNCHCTL, "print"): [_NOT_FOUND, _OK],
    }
    for key, queue in overrides.items():
        answers[(LAUNCHCTL, key)] = queue
    return answers


class InstallTest(unittest.TestCase):
    """Validate, record, load the agent, confirm."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()
        self.state_dir = self.home / "state"
        self.plist = self.home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        self.claude = self.home / "bin" / "claude"
        self.claude.parent.mkdir()
        self.claude.write_bytes(b"#!/bin/sh\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _install(
        self,
        answers: dict[tuple[str, str], list[RunResult]],
        *extra: str,
        claude: str | None = _REAL_CLAUDE,
    ) -> tuple[int, list[list[str]], str, str]:
        if claude == _REAL_CLAUDE:
            claude = str(self.claude)
        seams, runner, out, err = make_seams(self.home, answers, claude=claude)
        argv = ["--mode", "install", "--state-dir", str(self.state_dir), *extra]
        code = main(argv, seams=seams)
        return code, runner.commands(), out.getvalue(), err.getvalue()

    def test_install_records_config_and_loads_the_agent(self) -> None:
        code, commands, out, _err = self._install(_launchd_answers())
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(
            load_config(self.state_dir),
            PrimerConfig(
                None, "haiku", "Reply with the single word: ok", 5, str(self.claude)
            ),
        )
        job = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(
            job["ProgramArguments"],
            [
                "/opt/fake/bin/python3",
                "/opt/fake/usage_window_primer.py",
                "--mode",
                "tick",
                "--state-dir",
                str(self.state_dir),
            ],
        )
        self.assertEqual(commands[-1], [LAUNCHCTL, "print", f"gui/501/{LAUNCHD_LABEL}"])
        self.assertFalse((self.state_dir / STATE_FILENAME).exists())
        self.assertIn("Installed the launchd agent", out)

    def test_install_defaults_the_state_dir_to_local_app_data_on_windows(
        self,
    ) -> None:
        # What is under test is where config.json lands. win32 selects the Task
        # Scheduler backend, which refuses this interpreter for having no pythonw.exe
        # beside it -- but only after config.json is recorded, and before any schtasks
        # call, so the runner is given no answers at all.
        local = self.home / "AppData" / "Local"
        # The backend stats the real filesystem, so the interpreter sits in a folder
        # this test made without pythonw.exe. Off Windows the backend reads the path
        # as a Windows one, which names a relative file; running from the temp home
        # keeps that lookup inside a folder this test owns too.
        python = self.home / "Python" / "python.exe"
        python.parent.mkdir()
        python.write_bytes(b"")
        seams, runner, _out, err = make_seams(
            self.home,
            {},
            claude=str(self.claude),
            platform="win32",
            environ={"LOCALAPPDATA": str(local)},
            python=str(python),
        )
        with contextlib.chdir(self.home):
            code = main(["--mode", "install"], seams=seams)
        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("pythonw.exe", err.getvalue())
        self.assertEqual(runner.calls, [])
        self.assertTrue((local / "usage_window_primer" / CONFIG_FILENAME).exists())

    def test_install_with_start_and_last_run(self) -> None:
        extra = (
            "--start",
            "2026-01-02T06:00:00+00:00",
            "--last-run",
            "2026-01-01T09:30:00+00:00",
            "--poll-minutes",
            "15",
        )
        code, _commands, _out, _err = self._install(_launchd_answers(), *extra)
        self.assertEqual(code, EXIT_OK)
        config = load_config(self.state_dir)
        assert config is not None
        self.assertEqual(
            (config.start, config.poll_minutes),
            (datetime(2026, 1, 2, 6, 0, tzinfo=UTC), 15),
        )
        state = load_state(self.state_dir)
        last_run = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        self.assertEqual(
            (state.window_ends_at, state.window_end_source, state.last_ping_at),
            (last_run + timedelta(hours=5), "manual", last_run),
        )
        job = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(len(job["StartCalendarInterval"]), 4)

    def test_install_resolves_the_state_dir(self) -> None:
        roundabout = f"{self.home}/sub/../state"
        seams, _runner, _out, _err = make_seams(
            self.home, _launchd_answers(), claude=str(self.claude)
        )
        code = main(["--mode", "install", "--state-dir", roundabout], seams=seams)
        self.assertEqual(code, EXIT_OK)
        job = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(job["ProgramArguments"][-1], str(self.state_dir))

    def test_install_records_an_absolute_claude_path(self) -> None:
        # launchd runs the tick from another working directory, so a relative PATH
        # entry must not leave a relative claude_path behind.
        relative = self.home / "relative" / "bin" / "claude"
        relative.parent.mkdir(parents=True)
        relative.write_bytes(b"#!/bin/sh\n")
        with contextlib.chdir(self.home):
            code, _commands, _out, _err = self._install(
                _launchd_answers(), claude="relative/bin/claude"
            )
        self.assertEqual(code, EXIT_OK)
        config = load_config(self.state_dir)
        assert config is not None
        self.assertTrue(os.path.isabs(config.claude_path))
        self.assertEqual(config.claude_path, str(relative))

    def test_install_rejects_bad_settings_before_touching_anything(self) -> None:
        cases: dict[str, tuple[str, ...]] = {
            "poll minutes that do not divide 60": ("--poll-minutes", "7"),
            "a start that is not ISO 8601": ("--start", "tomorrow"),
            "a start outside the supported range": (
                "--start",
                "9999-12-31T23:59-01:00",
            ),
            "a last run that is not ISO 8601": ("--last-run", "earlier"),
            # The = form: argparse would reject a separate "-x" value on its own.
            "a prompt claude would read as an option": ("--prompt=-x",),
            "an empty prompt": ("--prompt=",),
            "a whitespace-only prompt": ("--prompt=   ",),
            # One word can name a claude subcommand instead of being sent as a message.
            "a one-word prompt": ("--prompt=update",),
            # One word with trailing whitespace is still one word.
            "a one-word prompt with a trailing space": ("--prompt=doctor ",),
            # Regression: config.json refuses a control character at every tick, so an
            # install that recorded one exited 0 and left every tick exiting 2.
            "a prompt holding a terminal escape": (f"--prompt=hello{_ESC} world",),
            "a model holding a terminal escape": (f"--model=haiku{_ESC}[2J",),
            # Regression: a future last run was recorded, and its window held every
            # tick back until five hours after that moment.
            "a last run in the future": (
                "--last-run",
                to_utc_iso(FIXED_NOW + timedelta(minutes=1)) or "",
            ),
        }
        for name, extra in cases.items():
            with self.subTest(case=name):
                code, commands, _out, err = self._install({}, *extra)
                self.assertEqual((code, commands), (EXIT_USAGE, []))
                self.assertTrue(err.startswith("ERROR:"))
                self.assertFalse((self.state_dir / CONFIG_FILENAME).exists())

    def test_install_without_claude_on_path_exits_2(self) -> None:
        code, commands, _out, err = self._install({}, claude=None)
        self.assertEqual((code, commands), (EXIT_USAGE, []))
        self.assertIn("claude is not on PATH", err)

    def test_install_refuses_a_store_alias(self) -> None:
        # A 0-byte app execution alias cannot be launched by a scheduled task, so
        # every tick would fail; refusing at install says why while someone is watching.
        alias = self.home / "WindowsApps" / "claude.exe"
        alias.parent.mkdir()
        alias.write_bytes(b"")
        code, commands, _out, err = self._install({}, claude=str(alias))
        self.assertEqual((code, commands), (EXIT_USAGE, []))
        self.assertTrue(err.startswith("ERROR:"))
        self.assertIn("Store", err)
        self.assertFalse((self.state_dir / CONFIG_FILENAME).exists())

    def test_install_refuses_a_cli_it_cannot_read(self) -> None:
        missing = str(self.home / "gone" / "claude")
        code, commands, _out, err = self._install({}, claude=missing)
        self.assertEqual((code, commands), (EXIT_USAGE, []))
        self.assertTrue(err.startswith("ERROR:"))
        self.assertIn("cannot be read", err)
        self.assertFalse((self.state_dir / CONFIG_FILENAME).exists())

    def test_install_with_a_corrupt_state_exits_2_and_leaves_it(self) -> None:
        self.state_dir.mkdir()
        (self.state_dir / STATE_FILENAME).write_bytes(b"{broken")
        code, commands, _out, _err = self._install(
            {}, "--last-run", "2026-01-01T09:30:00+00:00"
        )
        self.assertEqual((code, commands), (EXIT_USAGE, []))
        self.assertEqual((self.state_dir / STATE_FILENAME).read_bytes(), b"{broken")

    def test_install_accepts_a_last_run_of_exactly_now(self) -> None:
        code, _commands, _out, err = self._install(
            _launchd_answers(), "--last-run", to_utc_iso(FIXED_NOW) or ""
        )
        self.assertEqual(code, EXIT_OK, err)

    def test_install_refuses_a_batch_shim_on_windows(self) -> None:
        # Regression: a claude.cmd was recorded, and every ping then ran through cmd.exe.
        shim = self.home / "npm" / "claude.cmd"
        shim.parent.mkdir()
        shim.write_bytes(b"@echo off\r\n")
        seams, runner, _out, err = make_seams(
            self.home, {}, claude=str(shim), platform="win32"
        )
        argv = ["--mode", "install", "--state-dir", str(self.state_dir)]
        self.assertEqual(main(argv, seams=seams), EXIT_USAGE)
        self.assertIn("claude.exe", err.getvalue())
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.state_dir / CONFIG_FILENAME).exists())

    @unittest.skipUnless(_POSIX_HOST, _NEEDS_POSIX)
    def test_install_creates_a_missing_state_dir_private(self) -> None:
        code, _commands, _out, err = self._install(_launchd_answers())
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)

    @unittest.skipUnless(_POSIX_HOST, _NEEDS_POSIX)
    def test_install_keeps_an_existing_state_dir_s_mode(self) -> None:
        # The live state dir predates this check at 0755: it passes, and is left so.
        self.state_dir.mkdir(mode=0o755)
        self.state_dir.chmod(0o755)
        code, _commands, _out, err = self._install(_launchd_answers())
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o755)

    @unittest.skipUnless(_POSIX_HOST, _NEEDS_POSIX)
    def test_install_refuses_a_state_dir_others_can_write(self) -> None:
        # Regression: a group- or world-writable state dir let another account replace
        # config.json, whose claude_path every tick then runs.
        for mode in (0o770, 0o707, 0o777):
            with self.subTest(mode=oct(mode)):
                self.state_dir.mkdir(exist_ok=True)
                self.state_dir.chmod(mode)
                code, commands, _out, err = self._install({})
                self.assertEqual((code, commands), (EXIT_USAGE, []))
                self.assertIn("writable by other users", err)
                self.assertFalse((self.state_dir / CONFIG_FILENAME).exists())

    @unittest.skipUnless(_POSIX_HOST, _NEEDS_POSIX)
    def test_install_refuses_a_state_dir_another_user_owns(self) -> None:
        self.state_dir.mkdir(mode=0o700)
        seams, runner, _out, err = make_seams(self.home, {}, claude=str(self.claude))
        owner = self.state_dir.stat().st_uid
        seams = dataclasses.replace(seams, euid=lambda: owner + 1)
        argv = ["--mode", "install", "--state-dir", str(self.state_dir)]
        self.assertEqual(main(argv, seams=seams), EXIT_USAGE)
        self.assertIn("not owned by", err.getvalue())
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.state_dir / CONFIG_FILENAME).exists())

    def test_install_exits_1_when_launchd_refuses(self) -> None:
        answers = _launchd_answers(bootstrap=[(5, "", "Bootstrap failed: 5")])
        code, _commands, _out, err = self._install(answers)
        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("bootstrap failed", err)
        # Recorded before the load, so a refused bootstrap leaves the config on disk.
        self.assertTrue((self.state_dir / CONFIG_FILENAME).exists())


class UninstallTest(unittest.TestCase):
    """Unload, confirm, remove the plist, keep the state."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()
        self.state_dir = self.home / "state"
        self.plist = self.home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        self.plist.parent.mkdir(parents=True)
        self.plist.write_bytes(b"<plist/>")
        self.state_dir.mkdir()
        (self.state_dir / STATE_FILENAME).write_text(
            "{}", encoding="utf-8", newline="\n"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_uninstall_removes_the_agent_and_keeps_state(self) -> None:
        answers = _launchd_answers(print=[_NOT_FOUND])
        seams, _runner, out, _err = make_seams(self.home, answers)
        code = main(
            ["--mode", "uninstall", "--state-dir", str(self.state_dir)], seams=seams
        )
        self.assertEqual(code, EXIT_OK)
        self.assertFalse(self.plist.exists())
        self.assertTrue((self.state_dir / STATE_FILENAME).exists())
        self.assertIn("Uninstalled the launchd agent", out.getvalue())

    def test_uninstall_exits_1_when_the_user_id_is_unreadable(self) -> None:
        seams, _runner, _out, err = make_seams(
            self.home, {(ID_COMMAND, "-u"): [(1, "", "id: failed")]}
        )
        code = main(
            ["--mode", "uninstall", "--state-dir", str(self.state_dir)], seams=seams
        )
        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("could not read the user id", err.getvalue())
        self.assertTrue(self.plist.exists())


# One /Query row as schtasks /FO CSV /NH prints it: TaskName, Next Run Time, Status.
_QUERY_ROW: RunResult = (0, '"\\UsageWindowPrimer\\Tick","N/A","Ready"\n', "")
_QUERY_GONE: RunResult = (1, "", "ERROR: The system cannot find the file specified.\n")


class WindowsLifecycleTest(unittest.TestCase):
    """win32 install, status and uninstall through main, against a scripted schtasks."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()
        self.local = self.home / "AppData" / "Local"
        self.claude = self.home / "bin" / "claude"
        self.claude.parent.mkdir()
        self.claude.write_bytes(b"#!/bin/sh\n")
        # A python.org layout: pythonw.exe beside the interpreter that runs install.
        self.python = self.home / "Python" / "python.exe"
        self.python.parent.mkdir()
        for name in ("python.exe", "pythonw.exe"):
            (self.python.parent / name).write_bytes(b"")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _main(
        self, mode: str, answers: dict[tuple[str, str], list[RunResult]]
    ) -> tuple[int, str, str]:
        seams, _runner, out, err = make_seams(
            self.home,
            answers,
            claude=str(self.claude),
            platform="win32",
            environ={
                "LOCALAPPDATA": str(self.local),
                "USERDOMAIN": "<domain>",
                "USERNAME": "<user>",
            },
            python=str(self.python),
        )
        with contextlib.chdir(self.home):
            code = main(["--mode", mode], seams=seams)
        return code, out.getvalue(), err.getvalue()

    def test_install_status_and_uninstall_drive_the_task(self) -> None:
        # Off Windows the backend reads the interpreter's path as a Windows one, which
        # names a single relative file in the working directory; this places that file
        # in the temp home the test runs from. On Windows it is the file made above.
        with contextlib.chdir(self.home):
            Path(str(PureWindowsPath(self.python).with_name("pythonw.exe"))).touch()
        state_dir = self.local / "usage_window_primer"
        code, out, err = self._main(
            "install",
            {(_SCHTASKS, "/Create"): [_OK], (_SCHTASKS, "/Query"): [_QUERY_ROW]},
        )
        self.assertEqual(code, EXIT_OK, err)
        self.assertTrue((state_dir / "task.xml").is_file())
        self.assertIn("Installed the Task Scheduler task", out)
        code, out, err = self._main("status", {(_SCHTASKS, "/Query"): [_QUERY_ROW]})
        self.assertEqual(code, EXIT_OK, err)
        self.assertRegex(
            out, r"scheduler \(Task Scheduler task\) +registered \(Ready\)"
        )
        code, out, err = self._main(
            "uninstall",
            {(_SCHTASKS, "/Delete"): [_OK], (_SCHTASKS, "/Query"): [_QUERY_GONE]},
        )
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("Uninstalled the Task Scheduler task", out)


if __name__ == "__main__":
    unittest.main()
