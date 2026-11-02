"""The usage_window_primer's launchd user agent: build its plist, load and unload it, confirm both.

The schedule is ``StartCalendarInterval``, one ``{Minute: m}`` entry per poll slot,
and deliberately not ``StartInterval``: `man launchd.plist` documents that an interval
job misses firings while the machine sleeps, whereas calendar firings missed during sleep
coalesce into one firing on wake, and the catch-up ping depends on that. ``RunAtLoad``
covers a power-off, which no calendar firing survives.

Every command goes through the injected `Runner`, so no test touches the real agent
list. Loading and unloading are both confirmed by polling ``launchctl print`` rather than
trusting a command's own exit code, and a ``print`` that fails outright (a timeout, an
unrunnable launchctl) is read as no answer at all, never as "not loaded", which would
let an uninstall report success while the agent is still running.

`LaunchdBackend` puts these functions behind the `scheduler.SchedulerBackend` interface,
which is all the CLI calls.
"""

from __future__ import annotations

import plistlib
import time
from collections.abc import Callable
from pathlib import Path

from process_run import Runner

# AgentError is the error every backend shares.
from scheduler import (
    MINUTES_PER_HOUR,
    AgentError,
    InstallRequest,
    validate_poll_minutes,
)
from state_store import write_bytes_atomic

# The key launchd looks the job up by: a literal name only because launchd requires one,
# kept in this one place.
LAUNCHD_LABEL = "local.usage-window-primer"
# Both run by their absolute macOS paths, never looked up on PATH, so no folder earlier
# on PATH can choose which launchctl loads the agent or which id names its domain.
LAUNCHCTL = "/bin/launchctl"
ID_COMMAND = "/usr/bin/id"
LAUNCHCTL_TIMEOUT_SECONDS = 30.0
# launchctl's "Could not find service" exit: the label is not loaded in the domain.
LAUNCHCTL_NOT_FOUND_EXIT = 113
# bootout of a running job can take a moment to settle, so confirmation polls.
CONFIRM_ATTEMPTS = 5
CONFIRM_INTERVAL_SECONDS = 0.5
STDOUT_FILENAME = "launchd.out.log"
STDERR_FILENAME = "launchd.err.log"


def plist_path(home: Path) -> Path:
    """Where the user agent's plist lives under *home*."""
    return home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def calendar_minutes(poll_minutes: int) -> list[int]:
    """The minute marks a *poll_minutes* schedule fires on.

    Raises ValueError unless *poll_minutes* divides 60 (`validate_poll_minutes`).
    """
    validate_poll_minutes(poll_minutes)
    return list(range(0, MINUTES_PER_HOUR, poll_minutes))


def build_plist(python: str, script: str, state_dir: Path, poll_minutes: int) -> bytes:
    """The agent plist: one tick of *script* on every poll slot, and one at load."""
    job: dict[str, object] = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [
            python,
            script,
            "--mode",
            "tick",
            "--state-dir",
            str(state_dir),
        ],
        "StartCalendarInterval": [
            {"Minute": minute} for minute in calendar_minutes(poll_minutes)
        ],
        "RunAtLoad": True,
        "StandardOutPath": str(state_dir / STDOUT_FILENAME),
        "StandardErrorPath": str(state_dir / STDERR_FILENAME),
    }
    return plistlib.dumps(job, sort_keys=True)


def _launchctl(run: Runner, *args: str) -> tuple[int, str, str]:
    """Run one ``launchctl`` subcommand with the standard timeout."""
    return run([LAUNCHCTL, *args], LAUNCHCTL_TIMEOUT_SECONDS, None, None)


def user_domain(run: Runner) -> str:
    """The launchd GUI domain of the invoking user, read from ``id -u``.

    Read through the runner rather than `os.getuid`, which typeshed declares only off
    Windows: the gate type-checks for win32 too, and a platform-narrowed branch there
    could never be covered.
    """
    code, out, err = run([ID_COMMAND, "-u"], LAUNCHCTL_TIMEOUT_SECONDS, None, None)
    uid = out.strip()
    if code != 0 or not uid.isdigit():
        reason = err.strip() or uid
        raise AgentError(f"could not read the user id (exit {code}): {reason}")
    return f"gui/{uid}"


def agent_loaded(run: Runner, domain: str) -> bool:
    """Whether launchd currently has the agent loaded in *domain*.

    True on exit 0 and False on `LAUNCHCTL_NOT_FOUND_EXIT`. Any other exit (a timeout,
    an unrunnable launchctl) answers neither way, so it raises AgentError carrying the
    exit code and stderr instead of passing for "not loaded".
    """
    code, _out, err = _launchctl(run, "print", f"{domain}/{LAUNCHD_LABEL}")
    if code == 0:
        return True
    if code == LAUNCHCTL_NOT_FOUND_EXIT:
        return False
    raise AgentError(f"launchctl print failed (exit {code}): {err.strip()}")


def _wait_for_loaded(
    run: Runner, domain: str, *, loaded: bool, sleep: Callable[[float], None]
) -> None:
    """Poll until the agent's loaded state equals *loaded*; raise AgentError if it never does.

    A failed read counts as "not settled yet" and polling continues, so a transient
    launchctl error is retried rather than mistaken for either state. The final error
    names the wanted state and carries the last failed read, if there was one.
    """
    last_error: AgentError | None = None
    for _attempt in range(CONFIRM_ATTEMPTS):
        try:
            if agent_loaded(run, domain) == loaded:
                return
        except AgentError as exc:
            last_error = exc
        sleep(CONFIRM_INTERVAL_SECONDS)
    wanted = "loaded" if loaded else "not loaded"
    message = f"the agent was never read as {wanted} in {CONFIRM_ATTEMPTS} attempts"
    if last_error is not None:
        message = f"{message}; last read error: {last_error}"
    raise AgentError(message)


def _bootout_confirmed(
    run: Runner, domain: str, *, sleep: Callable[[float], None]
) -> None:
    """Boot the agent out of *domain* and confirm it is gone; raise AgentError if not."""
    # The exit code is ignored on purpose: booting out an agent that was never loaded
    # fails, which is the normal first install. The read-back decides, and bootout's
    # stderr is kept only to explain a read-back that never settles.
    _code, _out, err = _launchctl(run, "bootout", f"{domain}/{LAUNCHD_LABEL}")
    try:
        _wait_for_loaded(run, domain, loaded=False, sleep=sleep)
    except AgentError as exc:
        raise AgentError(f"{exc}; launchctl bootout said: {err.strip()}") from exc


def install_agent(
    plist_file: Path,
    plist_bytes: bytes,
    run: Runner,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Write the plist, (re)load the agent, and confirm launchd has it loaded."""
    domain = user_domain(run)
    write_bytes_atomic(plist_file, plist_bytes)
    # bootout first so a reinstall replaces cleanly, and wait for the old instance to
    # finish tearing down: bootstrap over a still-loaded job fails intermittently.
    _bootout_confirmed(run, domain, sleep=sleep)
    code, out, err = _launchctl(run, "bootstrap", domain, str(plist_file))
    if code != 0:
        reason = err.strip() or out.strip()
        raise AgentError(f"launchctl bootstrap failed (exit {code}): {reason}")
    _wait_for_loaded(run, domain, loaded=True, sleep=sleep)


def uninstall_agent(
    plist_file: Path,
    run: Runner,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Unload the agent, confirm it is gone, then remove its plist.

    The plist is removed only after a read-back confirms the unload, so a failed or
    unsettled read-back leaves it in place for a retry.
    """
    domain = user_domain(run)
    _bootout_confirmed(run, domain, sleep=sleep)
    plist_file.unlink(missing_ok=True)


class LaunchdBackend:
    """The launchd user agent as a `scheduler.SchedulerBackend`, over the functions above."""

    label = "launchd agent"

    def __init__(
        self,
        run: Runner,
        home: Path,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Drive launchctl through *run*; the plist lives under *home*."""
        self._run = run
        self._plist_file = plist_path(home)
        self._sleep = sleep

    def install(self, request: InstallRequest) -> None:
        """Write the plist for *request*, load it, and confirm launchd has it."""
        plist = build_plist(
            request.python, request.script, request.state_dir, request.poll_minutes
        )
        install_agent(self._plist_file, plist, self._run, sleep=self._sleep)

    def uninstall(self) -> None:
        """Unload the agent, confirm it is gone, then remove its plist."""
        uninstall_agent(self._plist_file, self._run, sleep=self._sleep)

    def describe_state(self) -> str:
        """``loaded``, ``not loaded``, or ``unknown (<error>)`` when launchd cannot be read."""
        try:
            loaded = agent_loaded(self._run, user_domain(self._run))
        except AgentError as exc:
            return f"unknown ({exc})"
        return "loaded" if loaded else "not loaded"
