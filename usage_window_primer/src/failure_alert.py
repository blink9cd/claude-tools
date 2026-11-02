"""Tell the person at this Mac when the usage window primer keeps failing, and when it recovers.

A tick that fails exits 1 and logs one line, but nothing reads the agent's log, state
or exit code: a failure streak from an expired login once ran for days unnoticed. This
module decides when a streak deserves a notification and shows it through `osascript`.

The threshold is `ALERT_AFTER_FAILURES`: at the default 5-minute poll that is an hour
of failed ticks while the Mac is awake, long enough that a brief outage clears without a
notification, short enough that a lasting fault such as an expired login is reported
within the hour. The counter a tick keeps only ever rises by one or resets to zero, so a
streak crosses the threshold exactly once and clears exactly once: one notification
each, never one per tick.

The notification text goes to `osascript` as argv, never spliced into the AppleScript
source, so no quoting layer exists to get wrong: the detail quoted from a failed ping is
the CLI's own text, and a quote or backslash in it cannot end a string literal it is
never inside.

Windows has no notification route yet: there `send_alert` runs nothing and returns
`UNSUPPORTED_ON_WIN32`, which the tick reports as its usual warning line.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from process_run import Runner

ALERT_AFTER_FAILURES = 12  # an hour of 5-minute ticks while awake
NOTIFY_TIMEOUT_SECONDS = 10.0
# Its absolute macOS path, never looked up on PATH, so no folder earlier on PATH can
# choose what runs with the alert's text.
OSASCRIPT = "/usr/bin/osascript"
# A notification is a glance, not a record: usage_window_primer.log carries the longer detail.
_MESSAGE_MAX_CHARS = 200
_FAILING_TITLE = "Claude usage window primer is failing"
_RECOVERED_TITLE = "Claude usage window primer recovered"
_EXIT_OK = 0
UNSUPPORTED_ON_WIN32 = "alert notifications are not supported on win32 yet"


@dataclass(frozen=True)
class Alert:
    """One notification: its title and its one-line message."""

    title: str
    message: str


def alert_for(before: int, after: int, detail: str) -> Alert | None:
    """The notification a tick owes, given the failure count *before* and *after* it.

    *detail* is the tick's outcome detail, quoted in a failing alert with every
    character that is not printable (NUL and the other control characters, newlines,
    tabs, a lone surrogate) turned into a space, and then its whitespace collapsed to
    single spaces. The detail is the CLI's own text: a NUL or a lone surrogate in argv
    makes `osascript` fail to start at all, the CLI's error message can span lines, and a
    message cut to its cap must not spend it on blank ones.
    """
    if before < ALERT_AFTER_FAILURES <= after:
        printable = "".join(c if c.isprintable() else " " for c in detail)
        latest = " ".join(printable.split())
        message = f"{after} pings in a row have failed. Latest: {latest}"
        return Alert(_FAILING_TITLE, message[:_MESSAGE_MAX_CHARS])
    if before >= ALERT_AFTER_FAILURES and after == 0:
        message = f"A ping got through after {before} failures in a row."
        return Alert(_RECOVERED_TITLE, message)
    return None


def is_failing(consecutive_failures: int) -> bool:
    """Whether *consecutive_failures* is a streak: the count the alert fires at, or more."""
    return consecutive_failures >= ALERT_AFTER_FAILURES


def build_notify_command(alert: Alert) -> list[str]:
    """The `osascript` command that shows *alert*, its text passed as arguments.

    A ``--`` separates the script from the arguments: without it `osascript` reads a
    title starting with ``-`` as one of its own options and exits 2 with no
    notification. Its option parsing stops at the first non-option argument, so only
    the title is exposed without it, but the separator protects whatever text either
    one holds. It is not passed to the script: the title is still item 1 of argv.
    """
    return [
        OSASCRIPT,
        "-e",
        "on run argv",
        "-e",
        "display notification (item 2 of argv) with title (item 1 of argv)",
        "-e",
        "end run",
        "--",
        alert.title,
        alert.message,
    ]


def send_alert(
    run: Runner, alert: Alert, *, platform: str = sys.platform
) -> str | None:
    """Show *alert* through *run*; None when `osascript` accepted it, else why not.

    Never raises for a failed run: *run* reports a missing `osascript` as exit 127 and a
    hung one as a timeout exit, and the caller only reports the problem. An alert from
    `alert_for` holds only printable text, so no NUL or lone surrogate in its argv can
    stop the command from starting. On win32 nothing is run and the answer is
    `UNSUPPORTED_ON_WIN32`.
    """
    # A plain comparison, not a pragma'd branch: both sides run on every host in tests.
    if platform == "win32":
        return UNSUPPORTED_ON_WIN32
    code, _out, err = run(
        build_notify_command(alert), NOTIFY_TIMEOUT_SECONDS, None, None
    )
    if code == _EXIT_OK:
        return None
    return f"exit {code}: {' '.join(err.split())}"
