"""The real subprocess seam, exercised against real child processes.

`RunProcessTest` holds live tests of the process boundary: every
child is this same interpreter running a one-line script, so nothing outside the test is
touched. `TimeoutEvidenceTest` is not live: it fakes `bounded_run.run_bounded` to expire
carrying chosen output, because a real child cannot be made to leave each shape of
partial output a timeout can recover.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import NamedTuple
from unittest import mock

_TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TOOL_ROOT / "src"))

import process_run  # noqa: E402 - import follows the sys.path bootstrap above
from process_run import (  # noqa: E402 - same bootstrap
    NOT_RUNNABLE_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    run_process,
)

# Well above the grandchild timeout plus a tree kill, well below the grandchild's 20-second
# sleep: only a bound that holds against the grandchild finishes inside it.
_GRANDCHILD_BOUND_SECONDS = 10.0
# The timeout every grandchild run gets. The child's interpreter start, imports, Popen and
# both pid writes must all finish inside it even on a loaded machine -- a kill that lands
# first leaves no grandchild to test. With the reap poll below it still ends well inside the
# grandchild's sleep, so a grandchild seen gone was killed rather than finished.
_GRANDCHILD_TIMEOUT_SECONDS = 3.0
# A killed process is re-parented and reaped asynchronously, so its exit is polled for.
_REAP_DEADLINE_SECONDS = 5.0
_REAP_POLL_SECONDS = 0.05
# The grandchild sleeps far past the timeout.
_GRANDCHILD = "import time; time.sleep(20)"
# argv[1] is the grandchild's script; argv[2] and argv[3] are the files the CHILD writes its
# own pid and then the grandchild's pid to. Popen knows the grandchild's pid at once, so both
# files exist before the timeout however long the grandchild's own interpreter takes to start.
# The child's own pid is for cleanup: a tree kill that regressed would leave the child running.
_SPAWN_GRANDCHILD = (
    "import os, pathlib, subprocess, sys, time; "
    "pathlib.Path(sys.argv[2]).write_text(str(os.getpid()), encoding='utf-8', "
    "newline='\\n'); "
    "grandchild = subprocess.Popen([sys.executable, '-c', sys.argv[1]]); "
    "pathlib.Path(sys.argv[3]).write_text(str(grandchild.pid), encoding='utf-8', "
    "newline='\\n'); "
)
# The child then either outlives the timeout itself, or exits at once and leaves the
# grandchild as the only live member of its process group.
_CHILD = _SPAWN_GRANDCHILD + "time.sleep(20)"
_EXITING_CHILD = _SPAWN_GRANDCHILD + "sys.exit(0)"

# The timeout for a child that writes and then hangs. Its interpreter start and both writes
# must finish inside it even on a loaded machine, or there is nothing partial to keep.
_PARTIAL_OUTPUT_TIMEOUT_SECONDS = 3.0
# One flushed line on each stream, so both reach their pipes, then a sleep far past the bound.
_WRITES_THEN_HANGS = (
    "import sys, time; print('partial', flush=True); "
    "print('warn', file=sys.stderr, flush=True); time.sleep(30)"
)
# The timeout the faked expiries below report; run_process names its own argument instead.
_FAKE_TIMEOUT_SECONDS = 5.0


class _GrandchildRun(NamedTuple):
    """How one timed-out grandchild run ended, for the tests to assert on."""

    code: int
    out: str
    elapsed: float
    # False unless the grandchild was seen gone -- so always False on Windows, where it cannot
    # be probed and the tests asserting on it are skipped.
    grandchild_gone: bool


def _gone_within(pid: int, deadline_seconds: float) -> bool:
    """Whether process *pid* no longer exists at some point before the deadline."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(_REAP_POLL_SECONDS)
    return False


def _require_recorded_pid(pid_file: Path) -> int:
    """The pid the child recorded in *pid_file*, required as an assertion, not a bare read.

    The file is missing, or only partly written, when the timeout expired before the child
    got as far as starting its grandchild: a machine too loaded for the test's timing. That is
    reported as the finding it is, rather than as a FileNotFoundError traceback.
    """
    try:
        return int(pid_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        raise AssertionError(
            "child did not start the grandchild before the bound"
        ) from None


def _seen_gone(pid_file: Path) -> bool:
    """Whether the process recorded in *pid_file* is seen to exit within the reap deadline.

    The pid is required either way. Nothing is ever seen gone on Windows, where
    ``os.kill(pid, 0)`` terminates a process instead of probing it.
    """
    pid = _require_recorded_pid(pid_file)
    return sys.platform != "win32" and _gone_within(pid, _REAP_DEADLINE_SECONDS)


def _kill_unless_seen_gone(pid_file: Path, *, seen_gone: bool) -> None:
    """SIGKILL the process recorded in *pid_file* unless it was already seen gone.

    Cleanup for a failing run, so no live test leaves a process behind. A pid seen gone is
    never signaled: by now it may belong to an unrelated process. SIGKILL rather than SIGTERM,
    because a survivor can inherit an ignored SIGTERM from whatever launched the suite. Nothing
    is signaled on Windows, where no pid can be probed and so none is ever seen gone.
    """
    if sys.platform != "win32" and not seen_gone:
        with contextlib.suppress(FileNotFoundError, ValueError, ProcessLookupError):
            os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)


def _time_out_with_a_grandchild(child: str) -> _GrandchildRun:
    """Run *child* under the grandchild timeout and report how the run ended.

    Both recorded processes are probed right after the run, the child first, so no assertion
    can come between a process dying and the probe that sees it. On the way out, whatever the
    outcome, each one not seen gone is killed, so a failing run never leaks a process past
    the test.
    """
    with tempfile.TemporaryDirectory() as tmp:
        child_pid_file = Path(tmp) / "child.pid"
        grandchild_pid_file = Path(tmp) / "grandchild.pid"
        argv = [
            sys.executable,
            "-c",
            child,
            _GRANDCHILD,
            str(child_pid_file),
            str(grandchild_pid_file),
        ]
        child_gone = grandchild_gone = False
        try:
            started = time.monotonic()
            code, out, _err = run_process(argv, _GRANDCHILD_TIMEOUT_SECONDS, None, None)
            elapsed = time.monotonic() - started
            child_gone = _seen_gone(child_pid_file)
            grandchild_gone = _seen_gone(grandchild_pid_file)
            return _GrandchildRun(code, out, elapsed, grandchild_gone)
        finally:
            _kill_unless_seen_gone(child_pid_file, seen_gone=child_gone)
            _kill_unless_seen_gone(grandchild_pid_file, seen_gone=grandchild_gone)


class RunProcessTest(unittest.TestCase):
    """Exit code, both streams, environment, working directory, timeout, missing binary."""

    def test_returns_exit_code_and_both_streams(self) -> None:
        script = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
        code, out, err = run_process([sys.executable, "-c", script], 30.0, None, None)
        self.assertEqual((code, out.strip(), err.strip()), (3, "out", "err"))

    def test_passes_the_given_environment(self) -> None:
        script = (
            "import os; print(os.environ.get('USAGE_WINDOW_PRIMER_PROBE', 'absent'))"
        )
        env = {"USAGE_WINDOW_PRIMER_PROBE": "present"}
        code, out, _err = run_process([sys.executable, "-c", script], 30.0, env, None)
        self.assertEqual((code, out.strip()), (0, "present"))

    def test_runs_in_the_given_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = "import os; print(os.getcwd())"
            argv = [sys.executable, "-c", script]
            _code, out, _err = run_process(argv, 30.0, None, Path(tmp))
            self.assertEqual(Path(out.strip()).resolve(), Path(tmp).resolve())

    def test_a_child_that_outlives_the_timeout_is_reported_not_raised(self) -> None:
        script = "import time; time.sleep(30)"
        code, out, err = run_process([sys.executable, "-c", script], 0.5, None, None)
        self.assertEqual((code, out), (TIMEOUT_EXIT_CODE, ""))
        self.assertIn("timed out", err)

    def test_a_grandchild_holding_the_pipes_cannot_outlive_the_timeout(self) -> None:
        # The grandchild inherits the captured pipes. A timeout that kills only the
        # direct child and then re-reads those pipes waits for the grandchild instead:
        # under launchd that hangs the job, and every later firing is skipped.
        run = _time_out_with_a_grandchild(_CHILD)
        self.assertEqual((run.code, run.out), (TIMEOUT_EXIT_CODE, ""))
        self.assertLess(run.elapsed, _GRANDCHILD_BOUND_SECONDS)

    @unittest.skipIf(
        sys.platform == "win32",
        "os.kill(pid, 0) terminates a process on Windows instead of probing it",
    )
    def test_a_timeout_kills_the_grandchild_too(self) -> None:
        # On POSIX, subprocess.run's timeout kills the direct child and never re-reads
        # the pipes, so the test above passes there either way; what differs is that a
        # grandchild survives it. The child records the grandchild's pid for the probe.
        run = _time_out_with_a_grandchild(_CHILD)
        self.assertEqual(run.code, TIMEOUT_EXIT_CODE)
        self.assertTrue(run.grandchild_gone)

    @unittest.skipIf(
        sys.platform == "win32",
        "os.kill(pid, 0) terminates a process on Windows instead of probing it",
    )
    def test_a_timeout_after_the_child_exits_still_reports_a_timeout(self) -> None:
        # The child exits at once, leaving the grandchild holding the pipes. macOS will
        # not look up the process group of an exited, unreaped child, so a kill that went
        # through that lookup raised instead: the timeout came back as 127, "could not
        # start", and the grandchild kept running.
        run = _time_out_with_a_grandchild(_EXITING_CHILD)
        self.assertEqual(run.code, TIMEOUT_EXIT_CODE)
        self.assertTrue(run.grandchild_gone)

    def test_a_timeout_keeps_what_the_child_wrote_before_it_hung(self) -> None:
        # What a hung ping wrote is the only evidence of where it stopped, and it has to
        # survive the tree kill, so this runs a real child rather than a faked expiry.
        argv = [sys.executable, "-c", _WRITES_THEN_HANGS]
        result = run_process(argv, _PARTIAL_OUTPUT_TIMEOUT_SECONDS, None, None)
        timed_out = f"timed out after {_PARTIAL_OUTPUT_TIMEOUT_SECONDS:g}s"
        self.assertEqual(
            result, (TIMEOUT_EXIT_CODE, "partial\n", f"{timed_out}; stderr: warn")
        )

    def test_a_missing_executable_is_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "no-such-claude")
            code, out, err = run_process([missing], 30.0, None, None)
        self.assertEqual((code, out), (NOT_RUNNABLE_EXIT_CODE, ""))
        self.assertIn("no-such-claude", err)

    def test_the_child_never_inherits_a_blocking_stdin(self) -> None:
        # A ping started from a pipe (a hook, a wrapper) must not wait on that pipe: point
        # this process's own fd 0 at a pipe whose write end stays open, so any read of an
        # inherited stdin blocks until the timeout instead of seeing end-of-file.
        read_end, write_end = os.pipe()
        saved_stdin = os.dup(0)
        try:
            os.dup2(read_end, 0)
            script = "import sys; sys.stdin.read(); print('done')"
            argv = [sys.executable, "-c", script]
            code, out, _err = run_process(argv, 10.0, None, None)
        finally:
            os.dup2(saved_stdin, 0)
            for fd in (saved_stdin, read_end, write_end):
                os.close(fd)
        self.assertEqual((code, out.strip()), (0, "done"))


def _run_expiring(
    output: str | bytes | None, stderr: str | bytes | None
) -> tuple[int, str, str]:
    """run_process against a bounded_run whose run expires carrying *output* and *stderr*."""
    expired = subprocess.TimeoutExpired(
        ["claude"], _FAKE_TIMEOUT_SECONDS, output=output, stderr=stderr
    )
    stand_in = mock.Mock(side_effect=expired)
    with mock.patch.object(process_run.bounded_run, "run_bounded", stand_in):
        return run_process(["claude"], _FAKE_TIMEOUT_SECONDS, None, None)


class TimeoutEvidenceTest(unittest.TestCase):
    """What a timeout keeps: the output bounded_run recovered after its tree kill."""

    def test_partial_stdout_and_stderr_are_kept(self) -> None:
        self.assertEqual(
            _run_expiring("partial\n", "warn\n"),
            (TIMEOUT_EXIT_CODE, "partial\n", "timed out after 5s; stderr: warn"),
        )

    def test_a_post_kill_read_that_expired_leaves_no_output(self) -> None:
        # bounded_run abandons the partial output when its own re-read times out.
        self.assertEqual(
            _run_expiring(None, None), (TIMEOUT_EXIT_CODE, "", "timed out after 5s")
        )

    def test_bytes_output_decodes_with_replacement(self) -> None:
        # run_bounded captures text, but TimeoutExpired is typed to allow bytes too.
        replaced = "\N{REPLACEMENT CHARACTER}"
        self.assertEqual(
            _run_expiring(b"x\xff", b"warn\xff\n"),
            (
                TIMEOUT_EXIT_CODE,
                f"x{replaced}",
                f"timed out after 5s; stderr: warn{replaced}",
            ),
        )

    def test_whitespace_only_stderr_adds_no_suffix(self) -> None:
        self.assertEqual(
            _run_expiring("", " \n\t"), (TIMEOUT_EXIT_CODE, "", "timed out after 5s")
        )


class ConsoleWindowTest(unittest.TestCase):
    """Every child starts without a console window.

    Harmless off win32, where the creation flags bounded_run passes the child are 0.
    """

    def test_every_run_asks_for_a_hidden_window(self) -> None:
        # Under pythonw.exe a console child (claude.exe, schtasks.exe) would otherwise
        # open a visible console on every tick.
        completed = subprocess.CompletedProcess(["claude"], 0, "out", "err")
        stand_in = mock.Mock(return_value=completed)
        with mock.patch.object(process_run.bounded_run, "run_bounded", stand_in):
            result = run_process(["claude"], _FAKE_TIMEOUT_SECONDS, None, None)
        self.assertEqual(result, (0, "out", "err"))
        self.assertIs(stand_in.call_args.kwargs["hide_window"], True)


if __name__ == "__main__":
    unittest.main()
