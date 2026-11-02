"""Run one external command with a hard timeout; return (exit code, stdout, stderr).

The single subprocess seam for the usage_window_primer tool: the ping (`ping_runner`), every
`id` / `launchctl` call (`launch_agent`), the failure notification's `osascript`
(`failure_alert`) and every `schtasks` call (`task_agent`) go through `run_process`, and
unit tests replace it with a fake of the same shape. A command that cannot finish, or that
the OS cannot start, comes back as a distinct exit code rather than an exception, so callers classify
one tuple instead of catching two exception families. An argument the OS cannot receive
at all (one holding a NUL) still raises ``ValueError`` before any child exists: that
is malformed input from the caller, not a failure of the command.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

# bounded_run, at one fixed place: its folder directly under the repository root, two
# levels above this file's own folder. It is what makes the timeout bind: it kills the
# child's whole process tree before touching the captured pipes, where subprocess.run
# kills only the direct child (bounded_run/bounded_run.md, "Why it exists"). A fixed path
# rather than a search of the parent directories, as in usage_window_primer.py; bounded_run puts its
# own stdio_utf8 on the path.
sys.path.append(str(Path(__file__).resolve().parents[2] / "bounded_run"))

import bounded_run

# The conventional shell codes for "timed out" (coreutils `timeout`) and "command not
# found", reused so a log line reads the way an operator already expects.
TIMEOUT_EXIT_CODE = 124
NOT_RUNNABLE_EXIT_CODE = 127

RunResult = tuple[int, str, str]
Runner = Callable[[list[str], float, Mapping[str, str] | None, Path | None], RunResult]


def _captured_text(value: str | bytes | None) -> str:
    """Output a TimeoutExpired carried, as text; run_bounded captures text, typeshed allows bytes."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_process(
    argv: list[str],
    timeout: float,
    env: Mapping[str, str] | None,
    cwd: Path | None,
) -> RunResult:
    """Run *argv* without a shell and return ``(exit_code, stdout, stderr)``.

    *env* ``None`` inherits this process's environment and *cwd* ``None`` its working
    directory. The child reads an empty stdin, so a ping started from a pipe never waits
    on it. Output is decoded as UTF-8 with replacement, so a stray byte from the child can
    never raise here. The child is started in its own session so a timeout can kill its
    whole tree; a consequence is that a launchd ``bootout`` during an in-flight ping ends
    the Python job but not its ``claude`` child, which exits on its own. On win32 the
    child also gets no console window of its own.

    A timeout returns `TIMEOUT_EXIT_CODE` with whatever the child wrote before the kill:
    its partial stdout (empty when none was recovered), and a stderr of ``timed out after
    <n>s``, followed by ``; stderr: <text>`` when the child had written any. A caller must
    test the exit code before reading stdout as a result.
    """
    try:
        # Every child without a console window: under pythonw.exe a console child
        # (claude.exe, schtasks.exe) would open a visible one on every tick. Off win32
        # the flag is a no-op, so it is passed unconditionally.
        completed = bounded_run.run_bounded(
            argv, timeout=timeout, cwd=cwd, env=env, hide_window=True
        )
    except subprocess.TimeoutExpired as exc:
        # The partial output is the only evidence of where a hung child stopped.
        message = f"timed out after {timeout:g}s"
        partial_err = _captured_text(exc.stderr).strip()
        if partial_err:
            message = f"{message}; stderr: {partial_err}"
        return TIMEOUT_EXIT_CODE, _captured_text(exc.stdout), message
    except OSError as exc:
        return NOT_RUNNABLE_EXIT_CODE, "", str(exc)
    return completed.returncode, completed.stdout, completed.stderr
