"""The scheduler interface: backend choice, the default state directory, the poll rule.

`LaunchdBackend` is driven through a ScriptedRunner, so no test touches the real agent
list.
"""

from __future__ import annotations

import plistlib
import sys
import tempfile
import unittest
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TOOL_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import RunResult, ScriptedRunner  # noqa: E402 - import follows the sys.path bootstrap above
from launch_agent import (  # noqa: E402 - same bootstrap
    ID_COMMAND,
    LAUNCHCTL,
    LAUNCHD_LABEL,
    LaunchdBackend,
    calendar_minutes,
)
from scheduler import (  # noqa: E402 - same bootstrap
    AgentError,
    InstallRequest,
    default_state_dir,
    select_backend,
    validate_poll_minutes,
)

_OK: RunResult = (0, "", "")
_NOT_FOUND: RunResult = (113, "", "Could not find service")
_TARGET = f"gui/501/{LAUNCHD_LABEL}"
_FAKE_LOCAL = "/opt/fake/AppData/Local"


def _answers(**overrides: list[RunResult]) -> dict[tuple[str, str], list[RunResult]]:
    answers: dict[tuple[str, str], list[RunResult]] = {
        (ID_COMMAND, "-u"): [(0, "501\n", "")],
        (LAUNCHCTL, "bootout"): [_OK],
        (LAUNCHCTL, "bootstrap"): [_OK],
        (LAUNCHCTL, "print"): [_OK],
    }
    for key, queue in overrides.items():
        answers[(LAUNCHCTL, key)] = queue
    return answers


class SelectBackendTest(unittest.TestCase):
    """Windows gets the Task Scheduler backend; every other platform gets launchd."""

    def test_windows_gets_task_scheduler(self) -> None:
        backend = select_backend(
            "win32",
            run=ScriptedRunner({}),
            home=Path("/opt/fake/home"),
            environ={},
        )
        self.assertEqual(backend.label, "Task Scheduler task")

    def test_non_windows_platforms_get_launchd(self) -> None:
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform):
                backend = select_backend(
                    platform,
                    run=ScriptedRunner({}),
                    home=Path("/opt/fake/home"),
                    environ={},
                )
                self.assertEqual(backend.label, "launchd agent")


class DefaultStateDirTest(unittest.TestCase):
    """LOCALAPPDATA on Windows when it is set, the home dot-folder otherwise."""

    home = Path("/opt/fake/home")

    def test_windows_uses_local_app_data(self) -> None:
        self.assertEqual(
            default_state_dir(self.home, {"LOCALAPPDATA": _FAKE_LOCAL}, "win32"),
            Path(_FAKE_LOCAL) / "usage_window_primer",
        )

    def test_windows_without_local_app_data_uses_home(self) -> None:
        for environ in ({}, {"LOCALAPPDATA": ""}):
            with self.subTest(environ=environ):
                self.assertEqual(
                    default_state_dir(self.home, environ, "win32"),
                    self.home / ".usage_window_primer",
                )

    def test_macos_ignores_local_app_data(self) -> None:
        self.assertEqual(
            default_state_dir(self.home, {"LOCALAPPDATA": _FAKE_LOCAL}, "darwin"),
            self.home / ".usage_window_primer",
        )


class ValidatePollMinutesTest(unittest.TestCase):
    """Both backends share one rule: the interval divides an hour."""

    def test_a_divisor_of_sixty_passes(self) -> None:
        validate_poll_minutes(5)

    def test_a_non_divisor_or_zero_raises(self) -> None:
        for poll in (7, 0):
            with self.subTest(poll_minutes=poll), self.assertRaises(ValueError):
                validate_poll_minutes(poll)

    def test_calendar_minutes_keeps_the_same_message(self) -> None:
        with self.assertRaises(ValueError) as caught:
            calendar_minutes(7)
        self.assertEqual(
            str(caught.exception), "--poll-minutes must divide 60 evenly, got 7"
        )


class LaunchdBackendTest(unittest.TestCase):
    """The adapter runs the launchd functions it wraps, unchanged."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.plist = self.home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        self.sleeps: list[float] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _backend(self, runner: ScriptedRunner) -> LaunchdBackend:
        return LaunchdBackend(runner, self.home, sleep=self.sleeps.append)

    def test_install_writes_the_plist_and_loads_it(self) -> None:
        runner = ScriptedRunner(
            _answers(bootout=[(5, "", "not loaded")], print=[_NOT_FOUND, _OK])
        )
        request = InstallRequest(
            "/opt/fake/python3",
            "/opt/fake/usage_window_primer.py",
            Path("/opt/fake/state"),
            15,
        )
        self._backend(runner).install(request)
        job = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(job["ProgramArguments"][0], "/opt/fake/python3")
        self.assertEqual(len(job["StartCalendarInterval"]), 4)
        self.assertEqual(
            runner.commands(),
            [
                [ID_COMMAND, "-u"],
                [LAUNCHCTL, "bootout", _TARGET],
                [LAUNCHCTL, "print", _TARGET],
                [LAUNCHCTL, "bootstrap", "gui/501", str(self.plist)],
                [LAUNCHCTL, "print", _TARGET],
            ],
        )
        self.assertEqual(self.sleeps, [])

    def test_uninstall_unloads_and_removes_the_plist(self) -> None:
        self.plist.parent.mkdir(parents=True)
        self.plist.write_bytes(b"<plist/>")
        self._backend(ScriptedRunner(_answers(print=[_NOT_FOUND]))).uninstall()
        self.assertFalse(self.plist.exists())

    def test_uninstall_raises_the_shared_error(self) -> None:
        runner = ScriptedRunner({(ID_COMMAND, "-u"): [(1, "", "id: failed")]})
        with self.assertRaises(AgentError):
            self._backend(runner).uninstall()

    def test_describe_state_reads_launchctl_print(self) -> None:
        cases: dict[int, str] = {0: "loaded", 113: "not loaded"}
        for code, expected in cases.items():
            with self.subTest(exit=code):
                runner = ScriptedRunner(_answers(print=[(code, "", "")]))
                self.assertEqual(self._backend(runner).describe_state(), expected)

    def test_describe_state_is_unknown_on_any_other_exit(self) -> None:
        runner = ScriptedRunner(_answers(print=[(5, "", "Input/output error")]))
        state = self._backend(runner).describe_state()
        self.assertTrue(state.startswith("unknown ("), state)
        self.assertIn("Input/output error", state)


if __name__ == "__main__":
    unittest.main()
