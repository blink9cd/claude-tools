"""The one interface the usage_window_primer's CLI drives a scheduler through, and what it shares.

``usage_window_primer.py`` never names a scheduler: it asks `select_backend` for the one this
platform uses and calls only the `SchedulerBackend` methods. Every backend raises the
shared `AgentError` when the scheduler does not end up in the state it was asked for,
and checks the poll interval with the shared `validate_poll_minutes`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from process_run import Runner

MINUTES_PER_HOUR = 60
# The state directory's name under the home folder: the default everywhere but Windows.
HOME_STATE_DIRNAME = ".usage_window_primer"
# Its name under %LOCALAPPDATA%, where Windows tools keep per-user state; a dot folder
# in the profile root is not hidden in Explorer.
LOCAL_APP_DATA_STATE_DIRNAME = "usage_window_primer"
LOCAL_APP_DATA_VARIABLE = "LOCALAPPDATA"


class AgentError(Exception):
    """The scheduler did not end up in the state a command asked for."""


@dataclass(frozen=True)
class InstallRequest:
    """What a backend needs to schedule a tick: the command and how often to run it."""

    python: str
    """The interpreter that ran install (``sys.executable``)."""
    script: str
    """The absolute path of usage_window_primer.py."""
    state_dir: Path
    """The absolute state directory every tick is pointed at."""
    poll_minutes: int


class SchedulerBackend(Protocol):
    """A scheduler that runs ``--mode tick`` on every poll slot."""

    label: str
    """How ``status`` and the install messages name the job, e.g. ``launchd agent``."""

    def install(self, request: InstallRequest) -> None:
        """Schedule the tick and confirm the scheduler has it; raise AgentError if not."""
        ...

    def uninstall(self) -> None:
        """Remove the job and confirm it is gone; raise AgentError if not."""
        ...

    def describe_state(self) -> str:
        """The job's state as ``status`` prints it. Never raises."""
        ...


def validate_poll_minutes(poll_minutes: int) -> None:
    """Raise ValueError unless *poll_minutes* divides 60.

    Both backends share the rule: launchd's calendar entries repeat every hour, so any
    other interval would leave an uneven gap at the top of each hour.
    """
    if poll_minutes < 1 or MINUTES_PER_HOUR % poll_minutes != 0:
        raise ValueError(
            f"--poll-minutes must divide {MINUTES_PER_HOUR} evenly, got {poll_minutes}"
        )


def default_state_dir(home: Path, environ: Mapping[str, str], platform: str) -> Path:
    """%LOCALAPPDATA%/usage_window_primer on win32 when it is set, else home/.usage_window_primer.

    An empty LOCALAPPDATA counts as unset: ``Path("")`` would put the state in the
    working directory, which a scheduled tick does not share with the install.
    """
    local_app_data = environ.get(LOCAL_APP_DATA_VARIABLE, "")
    if platform == "win32" and local_app_data:
        return Path(local_app_data) / LOCAL_APP_DATA_STATE_DIRNAME
    return home / HOME_STATE_DIRNAME


def select_backend(
    platform: str, *, run: Runner, home: Path, environ: Mapping[str, str]
) -> SchedulerBackend:
    """The backend for *platform*: Task Scheduler on win32, launchd everywhere else.

    *home* places launchd's plist; *environ* names the Task Scheduler logon user.
    """
    # Both backends are imported here rather than at the top: each imports this module's
    # AgentError, InstallRequest and validate_poll_minutes, so a top-level import would
    # be circular.
    # The branch tests the injected *platform*, not the host's sys.platform, so the
    # win32 install test reaches it on every host.
    if platform == "win32":
        from task_agent import TaskSchedulerBackend

        return TaskSchedulerBackend(run, environ)
    from launch_agent import LaunchdBackend

    return LaunchdBackend(run, home)
