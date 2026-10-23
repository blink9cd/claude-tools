r"""The usage_window_primer's Windows Task Scheduler task: render its XML, register it, confirm both.

One task with two triggers: a time trigger repeating every poll interval indefinitely, so
a window that ends inside a session is caught (a logon trigger alone would miss it), and a
logon trigger, the twin of launchd's ``RunAtLoad``, so a power-off costs no poll interval.
Its principal is ``InteractiveToken`` at least privilege, so it runs only inside the user's
signed-in session and installing it needs no elevation. Not ``S4U``: its token has no
network access, so every ping would fail, and registering one needs elevation. Its command
is ``pythonw.exe`` beside the interpreter that ran install, so a tick never flashes a
console window.

Every ``schtasks`` command goes through the injected `Runner`, so no test touches the real
Task Scheduler. Registration and removal are both confirmed by polling ``schtasks /Query``
(act, then confirm) rather than trusting the command's own exit code. `TaskSchedulerBackend`
puts this behind the `scheduler.SchedulerBackend` interface, which is all the CLI calls.
"""

from __future__ import annotations

import csv
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path, PureWindowsPath

# schedule_artifacts, at ONE fixed place: its folder directly under the repository root,
# as process_run places bounded_run, and for the same reason; schedule_artifacts puts its
# own src/ on the path.
sys.path.append(str(Path(__file__).resolve().parents[2] / "schedule_artifacts"))

from launch_agent import CONFIRM_ATTEMPTS, CONFIRM_INTERVAL_SECONDS
from process_run import Runner
from schedule_artifacts import AtLogon, RepeatEvery, render_task_xml
from scheduler import AgentError, InstallRequest, validate_poll_minutes
from state_store import write_bytes_atomic

# The name Task Scheduler files the task under: a literal name only because Task
# Scheduler requires one, kept in this one place.
TASK_NAME = r"\UsageWindowPrimer\Tick"
TASK_XML_NAME = "task.xml"
TASK_DESCRIPTION = (
    "Keeps a Claude usage window running: one tick every poll slot and one at logon."
)
# schtasks.exe is run from the Windows folder, never looked up on PATH, so no folder
# earlier on PATH (or the working directory, which Windows searches first) can choose
# which schtasks registers the task. The folder comes from SYSTEMROOT, which Windows
# sets in every process; the fallback is its default install location, for a stripped
# environment that lacks it.
SYSTEM_ROOT_VARIABLE = "SYSTEMROOT"
SYSTEM_ROOT_FALLBACK = "C:\\Windows"
SCHTASKS_TIMEOUT_SECONDS = 30.0
# A tick is bounded by its 120 s ping timeout; this is only the backstop past it.
EXECUTION_LIMIT = "PT10M"
PYTHONW_NAME = "pythonw.exe"
LOGON_TYPE = "InteractiveToken"
USERDOMAIN_VARIABLE = "USERDOMAIN"
USERNAME_VARIABLE = "USERNAME"
# schtasks' wording for a task that is not registered. It has no distinct exit code, so
# the text decides, matched case-insensitively on both streams.
CANNOT_FIND = "cannot find"
# /Query /FO CSV /NH columns: TaskName, Next Run Time, Status.
STATUS_COLUMN = 2


def pythonw_beside(python: str, is_file: Callable[[Path], bool] = Path.is_file) -> str:
    """``pythonw.exe`` next to *python*; AgentError naming the problem if it is absent.

    The path is built as a Windows path on every host: this backend only runs on Windows,
    and a POSIX path would read the backslashes as part of one file name.
    """
    candidate = str(PureWindowsPath(python).with_name(PYTHONW_NAME))
    if not is_file(Path(candidate)):
        raise AgentError(
            f"no {PYTHONW_NAME} beside the interpreter that ran install ({candidate}); "
            "install with a python.org build of Python, which ships it"
        )
    return candidate


def schtasks_path(environ: Mapping[str, str]) -> str:
    r"""``%SYSTEMROOT%\System32\schtasks.exe``, from *environ*; an empty value is unset."""
    root = environ.get(SYSTEM_ROOT_VARIABLE, "") or SYSTEM_ROOT_FALLBACK
    return str(PureWindowsPath(root) / "System32" / "schtasks.exe")


def logon_user(environ: Mapping[str, str]) -> str:
    r"""``USERDOMAIN\USERNAME`` from *environ*; AgentError when either is missing."""
    domain = environ.get(USERDOMAIN_VARIABLE, "")
    user = environ.get(USERNAME_VARIABLE, "")
    missing = [
        name
        for name, value in ((USERDOMAIN_VARIABLE, domain), (USERNAME_VARIABLE, user))
        if not value
    ]
    if missing:
        raise AgentError(f"cannot name the logon user: {', '.join(missing)} is not set")
    return f"{domain}\\{user}"


def build_primer_task_xml(
    request: InstallRequest,
    *,
    pythonw: str,
    user: str,
    now_local: datetime,
    task_name: str = TASK_NAME,
) -> str:
    """The task XML for *request*: a repeating tick from this minute, and one at logon."""
    # Floored to the minute so the repeat slots fall on whole minutes, as launchd's do.
    start = now_local.replace(second=0, microsecond=0)
    arguments = subprocess.list2cmdline(
        [request.script, "--mode", "tick", "--state-dir", str(request.state_dir)]
    )
    return render_task_xml(
        task_name=task_name,
        description=TASK_DESCRIPTION,
        triggers=[RepeatEvery(request.poll_minutes, start), AtLogon(user)],
        logon_type=LOGON_TYPE,
        command=pythonw,
        arguments=arguments,
        working_directory=str(request.state_dir),
        execution_limit=EXECUTION_LIMIT,
    )


def _cannot_find(out: str, err: str) -> bool:
    """Whether schtasks reported the task as not registered, on either stream."""
    return CANNOT_FIND in f"{out}\n{err}".lower()


def _reason(out: str, err: str) -> str:
    """The explanation schtasks gave: stderr, else stdout."""
    return err.strip() or out.strip()


def _status_column(out: str) -> str | None:
    """The Status field of the first CSV row in *out*, or None when there is none."""
    rows = [row for row in csv.reader(out.splitlines()) if row]
    if not rows or len(rows[0]) <= STATUS_COLUMN:
        return None
    return rows[0][STATUS_COLUMN]


class TaskSchedulerBackend:
    """The Task Scheduler task as a `scheduler.SchedulerBackend`."""

    label = "Task Scheduler task"

    def __init__(
        self,
        run: Runner,
        environ: Mapping[str, str],
        *,
        task_name: str = TASK_NAME,
        now_local: Callable[[], datetime] = datetime.now,
        sleep: Callable[[float], None] = time.sleep,
        is_file: Callable[[Path], bool] = Path.is_file,
    ) -> None:
        """Drive schtasks through *run*; its folder and the logon user come from *environ*."""
        self._run = run
        self._environ = environ
        self._schtasks_exe = schtasks_path(environ)
        self._task_name = task_name
        self._now_local = now_local
        self._sleep = sleep
        self._is_file = is_file

    def _schtasks(self, *args: str) -> tuple[int, str, str]:
        """Run one schtasks subcommand against this task, with the standard timeout."""
        argv = [self._schtasks_exe, args[0], "/TN", self._task_name, *args[1:]]
        return self._run(argv, SCHTASKS_TIMEOUT_SECONDS, None, None)

    def _query(self) -> tuple[int, str, str]:
        """``schtasks /Query`` for this task, one CSV row and no header."""
        return self._schtasks("/Query", "/FO", "CSV", "/NH")

    def _wait_until(self, *, registered: bool) -> None:
        """Poll /Query until the task's registration equals *registered*, else raise.

        A query that neither lists the task nor says it cannot find it answers neither
        way, so polling continues and the last such answer explains the final error.
        """
        last_error = ""
        for _attempt in range(CONFIRM_ATTEMPTS):
            code, out, err = self._query()
            if code == 0:
                if registered:
                    return
            elif _cannot_find(out, err):
                if not registered:
                    return
            else:
                last_error = f"; last schtasks exit {code}: {_reason(out, err)}"
            self._sleep(CONFIRM_INTERVAL_SECONDS)
        wanted = "registered" if registered else "not registered"
        raise AgentError(
            f"the task was never read as {wanted} in {CONFIRM_ATTEMPTS} attempts"
            f"{last_error}"
        )

    def install(self, request: InstallRequest) -> None:
        """Write task.xml for *request*, register it, and confirm Task Scheduler has it."""
        pythonw = pythonw_beside(request.python, self._is_file)
        user = logon_user(self._environ)
        validate_poll_minutes(request.poll_minutes)
        xml = build_primer_task_xml(
            request,
            pythonw=pythonw,
            user=user,
            now_local=self._now_local(),
            task_name=self._task_name,
        )
        xml_file = request.state_dir / TASK_XML_NAME
        # The "utf-16" codec writes the byte-order mark, which schtasks /XML expects
        # with the declaration's UTF-16 encoding.
        write_bytes_atomic(xml_file, xml.encode("utf-16"))
        code, out, err = self._schtasks("/Create", "/XML", str(xml_file), "/F")
        if code != 0:
            raise AgentError(
                f"schtasks /Create failed (exit {code}): {_reason(out, err)}"
            )
        self._wait_until(registered=True)

    def uninstall(self) -> None:
        """Delete the task and confirm it is gone; an already-absent task is success."""
        code, out, err = self._schtasks("/Delete", "/F")
        if code != 0 and not _cannot_find(out, err):
            raise AgentError(
                f"schtasks /Delete failed (exit {code}): {_reason(out, err)}"
            )
        self._wait_until(registered=False)
        # task.xml stays: Task Scheduler never reads it after /Create, so it is only the
        # record of what was registered, kept with the rest of the state directory.

    def describe_state(self) -> str:
        """``registered (<Status>)``, ``not registered``, or ``unknown (<error>)``."""
        code, out, err = self._query()
        if code == 0:
            status = _status_column(out)
            if status is None:
                return f"unknown (unreadable schtasks row: {out.strip()!r})"
            return f"registered ({status})"
        if _cannot_find(out, err):
            return "not registered"
        return f"unknown (schtasks exit {code}: {_reason(out, err)})"
