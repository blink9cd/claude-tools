"""The Task Scheduler backend: its task XML, install, uninstall and state, on any host.

Every schtasks command goes through a ScriptedRunner keyed on its first two words, so no
test touches the real Task Scheduler. The interpreter path and the
logon user are obvious fakes.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest

# The stdlib parser is safe here: it reads only XML this module itself rendered.
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TOOL_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import RunResult, ScriptedRunner  # noqa: E402 - import follows the sys.path bootstrap above
from launch_agent import CONFIRM_ATTEMPTS  # noqa: E402 - same bootstrap
from scheduler import AgentError, InstallRequest  # noqa: E402 - same bootstrap
from task_agent import (  # noqa: E402 - same bootstrap
    TASK_NAME,
    TASK_XML_NAME,
    TaskSchedulerBackend,
    build_primer_task_xml,
    logon_user,
    pythonw_beside,
    schtasks_path,
)

_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
_FAKE_PYTHON = "C:\\fake\\Python314\\python.exe"
_FAKE_PYTHONW = "C:\\fake\\Python314\\pythonw.exe"
_FAKE_USER = "FAKEDOMAIN\\fakeuser"
_ENVIRON = {"USERDOMAIN": "FAKEDOMAIN", "USERNAME": "fakeuser"}
_SCHTASKS = schtasks_path(_ENVIRON)
_NOW = datetime(2026, 1, 1, 9, 7, 42, 123456)
_OK: RunResult = (0, "", "")
_CANNOT_FIND: RunResult = (
    1,
    "",
    "ERROR: The system cannot find the file specified.\r\n",
)
_LISTED: RunResult = (0, '"\\UsageWindowPrimer\\Tick","N/A","Ready"\r\n', "")
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


def _answers(**queues: list[RunResult]) -> dict[tuple[str, str], list[RunResult]]:
    """ScriptedRunner answers for schtasks, keyed by subcommand name without its slash."""
    return {(_SCHTASKS, f"/{name}"): queue for name, queue in queues.items()}


def _backend(
    runner: ScriptedRunner,
    *,
    environ: dict[str, str] | None = None,
    pythonw_exists: bool = True,
) -> TaskSchedulerBackend:
    """A backend over *runner* with a fixed clock, a no-op sleep and a faked is_file."""
    return TaskSchedulerBackend(
        runner,
        _ENVIRON if environ is None else environ,
        now_local=lambda: _NOW,
        sleep=lambda _seconds: None,
        is_file=lambda _path: pythonw_exists,
    )


class TaskXmlTest(unittest.TestCase):
    """The rendered task: two triggers, the interactive principal, the pythonw command."""

    def setUp(self) -> None:
        self.state_dir = Path("C:/fake/Local/claude usage_window_primer")
        request = InstallRequest(
            _FAKE_PYTHON,
            "C:/fake/usage_window_primer/usage_window_primer.py",
            self.state_dir,
            5,
        )
        text = build_primer_task_xml(
            request, pythonw=_FAKE_PYTHONW, user=_FAKE_USER, now_local=_NOW
        )
        # Round-trip through UTF-16 bytes, the encoding install writes, so the XML
        # declaration's encoding must agree with what is on disk.
        self.root = ET.fromstring(text.encode("utf-16"))
        self.request = request

    def _text(self, path: str) -> str | None:
        return self.root.findtext(path, namespaces=_NS)

    def test_time_trigger_repeats_every_poll_interval(self) -> None:
        self.assertEqual(
            self._text("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval"), "PT5M"
        )

    def test_start_boundary_is_floored_to_the_minute(self) -> None:
        self.assertEqual(
            self._text("t:Triggers/t:TimeTrigger/t:StartBoundary"),
            "2026-01-01T09:07:00",
        )

    def test_logon_trigger_names_the_user(self) -> None:
        self.assertEqual(self._text("t:Triggers/t:LogonTrigger/t:UserId"), _FAKE_USER)

    def test_principal_is_interactive_token(self) -> None:
        self.assertEqual(
            self._text("t:Principals/t:Principal/t:LogonType"), "InteractiveToken"
        )

    def test_command_is_pythonw_and_arguments_are_quoted(self) -> None:
        self.assertEqual(self._text("t:Actions/t:Exec/t:Command"), _FAKE_PYTHONW)
        expected = subprocess.list2cmdline(
            [
                self.request.script,
                "--mode",
                "tick",
                "--state-dir",
                str(self.state_dir),
            ]
        )
        self.assertEqual(self._text("t:Actions/t:Exec/t:Arguments"), expected)
        self.assertEqual(
            self._text("t:Actions/t:Exec/t:WorkingDirectory"), str(self.state_dir)
        )

    def test_registration_uri_is_the_task_name(self) -> None:
        self.assertEqual(self._text("t:RegistrationInfo/t:URI"), TASK_NAME)

    def test_execution_time_limit_is_ten_minutes(self) -> None:
        self.assertEqual(self._text("t:Settings/t:ExecutionTimeLimit"), "PT10M")


class ValidationTest(unittest.TestCase):
    """pythonw.exe and the logon user, read before anything is written or run."""

    def test_pythonw_beside_returns_the_sibling(self) -> None:
        seen: list[Path] = []

        def is_file(path: Path) -> bool:
            seen.append(path)
            return True

        self.assertEqual(pythonw_beside(_FAKE_PYTHON, is_file), _FAKE_PYTHONW)
        self.assertEqual([str(path) for path in seen], [_FAKE_PYTHONW])

    def test_missing_pythonw_raises(self) -> None:
        with self.assertRaisesRegex(AgentError, "pythonw.exe"):
            pythonw_beside(_FAKE_PYTHON, lambda _path: False)

    def test_logon_user_joins_domain_and_name(self) -> None:
        self.assertEqual(logon_user(_ENVIRON), _FAKE_USER)

    def test_logon_user_missing_either_part_raises(self) -> None:
        for environ in ({"USERDOMAIN": "FAKEDOMAIN"}, {"USERNAME": "fakeuser"}, {}):
            with self.subTest(environ=environ):
                with self.assertRaisesRegex(AgentError, "USER"):
                    logon_user(environ)


class SchtasksPathTest(unittest.TestCase):
    """schtasks.exe is run from the Windows folder, never looked up on PATH."""

    def test_it_is_under_system_root(self) -> None:
        # Regression: it ran by bare name, so a schtasks earlier on PATH, or one in the
        # working directory, was the one that registered the task.
        environ = {"SYSTEMROOT": "D:\\fake\\Windows"}
        self.assertEqual(
            schtasks_path(environ), "D:\\fake\\Windows\\System32\\schtasks.exe"
        )

    def test_an_unset_or_empty_system_root_falls_back_to_the_default(self) -> None:
        for environ in ({}, {"SYSTEMROOT": ""}):
            with self.subTest(environ=environ):
                self.assertEqual(
                    schtasks_path(environ), "C:\\Windows\\System32\\schtasks.exe"
                )

    def test_the_backend_runs_that_path(self) -> None:
        environ = {**_ENVIRON, "SYSTEMROOT": "D:\\fake\\Windows"}
        runner = ScriptedRunner({(schtasks_path(environ), "/Query"): [_LISTED]})
        backend = TaskSchedulerBackend(runner, environ)
        self.assertEqual(backend.describe_state(), "registered (Ready)")
        self.assertEqual(runner.commands()[0][0], schtasks_path(environ))


class TaskNameTest(unittest.TestCase):
    """The task name is held to a Task Scheduler path before any schtasks call can see it."""

    def test_a_name_outside_the_path_shape_is_refused_at_construction(self) -> None:
        # render_task_xml escapes the name into <URI> but does not refuse it, and
        # schtasks reads /TN as a folder tree, so a `..` or empty segment would file the
        # task somewhere else. Each of these is refused before the backend exists.
        for name in (
            "UsageWindowPrimer\\Tick",
            "\\",
            "\\UsageWindowPrimer\\\\Tick",
            "\\..\\Tick",
            "\\Usage Window\\Tick",
            "\\<Primer>\\Tick",
        ):
            with self.subTest(name=name):
                runner = ScriptedRunner({})
                with self.assertRaisesRegex(ValueError, "task_name"):
                    TaskSchedulerBackend(runner, _ENVIRON, task_name=name)
                self.assertEqual(runner.commands(), [])

    def test_the_renderer_refuses_a_name_outside_the_path_shape(self) -> None:
        # The renderer is public on its own, so it holds the name as os_schedule_gen's
        # build_task_xml does, not only when a backend calls it.
        request = InstallRequest(
            _FAKE_PYTHON, "C:/fake/usage_window_primer.py", Path("C:/fake/state"), 5
        )
        for name in ("\\..\\Tick", "Tick", "\\<Primer>\\Tick"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "task_name"):
                    build_primer_task_xml(
                        request,
                        pythonw=_FAKE_PYTHONW,
                        user=_FAKE_USER,
                        now_local=_NOW,
                        task_name=name,
                    )

    def test_the_default_and_any_task_path_are_accepted(self) -> None:
        for name in (TASK_NAME, "\\Folder\\Task", "\\Task"):
            with self.subTest(name=name):
                TaskSchedulerBackend(ScriptedRunner({}), _ENVIRON, task_name=name)


class InstallTest(unittest.TestCase):
    """Write task.xml, register it with /Create, confirm it with /Query."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "state dir"
        self.request = InstallRequest(_FAKE_PYTHON, "C:/fake/k.py", self.state_dir, 5)

    def test_writes_utf16_xml_then_creates_and_confirms(self) -> None:
        runner = ScriptedRunner(_answers(Create=[_OK], Query=[_LISTED]))
        _backend(runner).install(self.request)
        xml_file = self.state_dir / TASK_XML_NAME
        self.assertTrue(xml_file.read_bytes().startswith(_UTF16_BOMS))
        commands = runner.commands()
        self.assertEqual([argv[1] for argv in commands], ["/Create", "/Query"])
        self.assertEqual(
            commands[0],
            [_SCHTASKS, "/Create", "/TN", TASK_NAME, "/XML", str(xml_file), "/F"],
        )
        self.assertEqual(
            commands[1], [_SCHTASKS, "/Query", "/TN", TASK_NAME, "/FO", "CSV", "/NH"]
        )

    def test_confirmation_polls_until_listed(self) -> None:
        runner = ScriptedRunner(_answers(Create=[_OK], Query=[_CANNOT_FIND, _LISTED]))
        _backend(runner).install(self.request)
        self.assertEqual(len(runner.commands()), 3)

    def test_create_failure_raises_with_stderr(self) -> None:
        runner = ScriptedRunner(
            _answers(Create=[(1, "", "ERROR: Access is denied.\r\n")])
        )
        with self.assertRaisesRegex(AgentError, r"exit 1.*Access is denied"):
            _backend(runner).install(self.request)

    def test_query_never_listed_raises_after_the_attempts(self) -> None:
        runner = ScriptedRunner(
            _answers(Create=[_OK], Query=[(2, "", "ERROR: busy\r\n")])
        )
        with self.assertRaisesRegex(AgentError, "busy"):
            _backend(runner).install(self.request)
        queries = [argv for argv in runner.commands() if argv[1] == "/Query"]
        self.assertEqual(len(queries), CONFIRM_ATTEMPTS)

    def test_missing_pythonw_raises_before_any_command(self) -> None:
        runner = ScriptedRunner({})
        with self.assertRaisesRegex(AgentError, "pythonw.exe"):
            _backend(runner, pythonw_exists=False).install(self.request)
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.state_dir / TASK_XML_NAME).exists())

    def test_missing_username_raises_before_any_command(self) -> None:
        runner = ScriptedRunner({})
        with self.assertRaisesRegex(AgentError, "USERNAME"):
            _backend(runner, environ={"USERDOMAIN": "FAKEDOMAIN"}).install(self.request)
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.state_dir / TASK_XML_NAME).exists())


class UninstallTest(unittest.TestCase):
    """Delete the task and confirm it is gone; task.xml stays as the install record."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)
        (self.state_dir / TASK_XML_NAME).write_bytes("<Task/>".encode("utf-16"))

    def _listing(self) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in self.state_dir.iterdir()}

    def test_success_confirms_gone_and_changes_no_file(self) -> None:
        before = self._listing()
        runner = ScriptedRunner(_answers(Delete=[_OK], Query=[_LISTED, _CANNOT_FIND]))
        _backend(runner).uninstall()
        self.assertEqual(
            runner.commands()[0], [_SCHTASKS, "/Delete", "/TN", TASK_NAME, "/F"]
        )
        self.assertEqual(len(runner.commands()), 3)
        self.assertEqual(self._listing(), before)

    def test_already_absent_counts_as_success(self) -> None:
        runner = ScriptedRunner(_answers(Delete=[_CANNOT_FIND], Query=[_CANNOT_FIND]))
        _backend(runner).uninstall()

    def test_cannot_find_on_stdout_is_also_recognized(self) -> None:
        on_stdout: RunResult = (1, "ERROR: The system CANNOT FIND the file.", "")
        runner = ScriptedRunner(_answers(Delete=[on_stdout], Query=[on_stdout]))
        _backend(runner).uninstall()

    def test_other_delete_failure_raises(self) -> None:
        runner = ScriptedRunner(
            _answers(Delete=[(1, "", "ERROR: Access is denied.\r\n")])
        )
        with self.assertRaisesRegex(AgentError, "Access is denied"):
            _backend(runner).uninstall()
        self.assertEqual(len(runner.commands()), 1)

    def test_task_that_stays_listed_raises(self) -> None:
        runner = ScriptedRunner(_answers(Delete=[_OK], Query=[_LISTED]))
        with self.assertRaisesRegex(AgentError, "not registered"):
            _backend(runner).uninstall()


class DescribeStateTest(unittest.TestCase):
    """``registered (<Status>)``, ``not registered`` or ``unknown (...)``; never raises."""

    def _describe(self, answer: RunResult) -> str:
        return _backend(ScriptedRunner(_answers(Query=[answer]))).describe_state()

    def test_listed_task_reports_its_status(self) -> None:
        self.assertEqual(self._describe(_LISTED), "registered (Ready)")

    def test_leading_blank_line_is_skipped(self) -> None:
        answer: RunResult = (0, "\r\n" + _LISTED[1], "")
        self.assertEqual(self._describe(answer), "registered (Ready)")

    def test_cannot_find_is_not_registered(self) -> None:
        self.assertEqual(self._describe(_CANNOT_FIND), "not registered")

    def test_other_failure_is_unknown(self) -> None:
        self.assertEqual(
            self._describe((1, "", "ERROR: Access is denied.\r\n")),
            "unknown (schtasks exit 1: ERROR: Access is denied.)",
        )

    def test_unreadable_row_is_unknown(self) -> None:
        for output in ("", '"only","two"\r\n'):
            with self.subTest(output=output):
                self.assertTrue(self._describe((0, output, "")).startswith("unknown ("))

    def test_label(self) -> None:
        self.assertEqual(TaskSchedulerBackend.label, "Task Scheduler task")


if __name__ == "__main__":
    unittest.main()
