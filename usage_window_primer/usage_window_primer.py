"""Keep a Claude subscription usage window running while this machine is unattended.

The CLI entry point. ``--mode install`` records the settings and schedules a job (a
launchd user agent on macOS) that runs ``--mode tick`` on every poll slot, at wake and at
login. A tick pings only once the recorded window end has passed, then records the end
the server reported. ``--mode status`` prints what is recorded; ``--mode uninstall``
removes the job and keeps the state directory. usage_window_primer.md holds the full contract.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

# stdio_utf8 and help_formatter, at one fixed place each: their folders directly under the
# repository root, this file's grandparent (stdio_utf8/stdio_utf8.md, "How this project's
# tools import it"). Appended, not inserted, so neither can shadow src/ or the standard
# library. Not a search of the parent directories: with a folder missing, a search would
# reach past the clone and import whatever same-named folder it found above it; a fixed
# path fails at the import instead.
sys.path += [
    str(Path(__file__).resolve().parents[1] / name)
    for name in ("stdio_utf8", "help_formatter")
]

import help_formatter
import stdio_utf8

# The private modules in src/: reachable only
# through this primary script, so the insert targets this file's own directory.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from failure_alert import alert_for, is_failing, send_alert
from headless_streams import missing_streams_opened
from install_checks import batch_shim_problem, locate_claude, validate_last_run
from install_checks import validate_model, validate_prompt
from ping_output import OUTCOME_FAILED, PingOutcome, parse_ping_output
from ping_runner import run_ping
from process_run import Runner, run_process
from scheduler import AgentError, InstallRequest, SchedulerBackend
from scheduler import default_state_dir, select_backend, validate_poll_minutes
from state_dir_guard import DefaultStateDir, effective_uid, ensure_private_dir
from state_dir_guard import state_dir_problem, state_dir_text_problem
from state_store import (
    CONFIG_FILENAME,
    PrimerConfig,
    PrimerState,
    StoreError,
    append_log,
    load_config,
    load_state,
    save_config,
    save_state,
    to_utc_iso,
)
from status_view import status_rows
from window_rules import (
    TickAction,
    apply_outcome,
    decide_tick,
    parse_user_datetime,
    record_manual_last_run,
    trusted_window_end,
)

DEFAULT_MODEL = "haiku"
DEFAULT_PROMPT = "Reply with the single word: ok"
DEFAULT_POLL_MINUTES = 5
# Spaces between the longest status label and the column every value starts in.
STATUS_LABEL_GAP = 2
# The longest detail one log line carries. A failed result's error message arrives from the
# CLI's stream uncapped, and the log must stay one bounded line per ping attempt.
LOG_DETAIL_MAX_CHARS = 300
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
MODES = ("install", "uninstall", "tick", "status")
# Whether the host running this process is a POSIX one. The state-dir guard reads the
# real filesystem, so it follows this, never the platform seam, which tests fake.
POSIX_HOST = os.name == "posix"


def _utc_now() -> datetime:
    """The current moment, in UTC."""
    return datetime.now(UTC)


def _which(name: str) -> str | None:
    """The absolute path *name* resolves to on PATH, or None."""
    return shutil.which(name)


@dataclass(frozen=True)
class Seams:
    """Everything a mode touches outside this process, injectable as one unit for tests."""

    run: Runner = run_process
    now: Callable[[], datetime] = _utc_now
    which: Callable[[str], str | None] = _which
    home: Path = field(default_factory=Path.home)
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    python: str = sys.executable
    script: str = str(Path(__file__).resolve())
    # None under pythonw.exe, which has no console; main swaps in log files then.
    out: TextIO | None = field(default_factory=lambda: sys.stdout)
    err: TextIO | None = field(default_factory=lambda: sys.stderr)
    platform: str = sys.platform
    euid: Callable[[], int] = effective_uid


@dataclass(frozen=True)
class BoundSeams(Seams):
    """The seams every handler takes: main has given each stream somewhere to go.

    Concrete streams, so the type checker refuses a handler that could print to None,
    which ``print`` would silently send to the real stdout instead.
    """

    out: TextIO = field(kw_only=True)
    err: TextIO = field(kw_only=True)


def bind_streams(seams: Seams, out: TextIO, err: TextIO) -> BoundSeams:
    """*seams* with *out* and *err* as its streams."""
    kept = {f.name: getattr(seams, f.name) for f in fields(seams)}
    return BoundSeams(**{**kept, "out": out, "err": err})


def build_parser(default_dir: Path) -> argparse.ArgumentParser:
    """The argument parser; *default_dir* is the state directory when none is given."""
    parser = argparse.ArgumentParser(
        description=(
            "Keep a Claude subscription usage window running while this machine is "
            "unattended."
        ),
        # The shared formatter, not the stock one: below 3.13 the stock one stamps
        # "(default: None)" on the required --mode, and the floor is 3.12. This variant
        # also hides the note on an option left unset, whose None no one can type.
        formatter_class=help_formatter.UnsetHidingFormatter,
        epilog=(
            "Prerequisites: macOS (a launchd user agent) or Windows (a Task "
            "Scheduler task), and the claude CLI on PATH and signed in to a "
            "subscription when --mode install runs.\n\n"
            "Run order: --mode install once, and again to change a setting; the "
            "scheduler then runs --mode tick by itself; --mode status any time; "
            "--mode uninstall to stop.\n\n"
            "Exit codes: 0 success; 1 a ping failed, the scheduler did not confirm "
            "the job, or --mode status found a failure streak; 2 bad arguments, no "
            "install, or an unreadable config or state file."
        ),
    )
    parser.add_argument("--mode", required=True, choices=MODES, help="What to do.")
    parser.add_argument(
        "--state-dir",
        type=Path,
        # A marker, not the Path itself, so a mode can tell an omitted option apart.
        default=DefaultStateDir(default_dir),
        help="Directory holding config.json, state.json and the log.",
    )
    parser.add_argument(
        "--start",
        default=None,
        help="install only: no ping before this ISO 8601 moment (local time without "
        "an offset).",
    )
    parser.add_argument(
        "--last-run",
        default=None,
        help="install only: an ISO 8601 moment a ping is known to have happened; the "
        "window end becomes it plus 5 hours until a real ping reports the true end.",
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL, help="install only: the model the ping uses."
    )
    parser.add_argument(
        "--prompt",
        default=DEFAULT_PROMPT,
        help="install only: the text the ping sends.",
    )
    parser.add_argument(
        "--poll-minutes",
        type=int,
        default=DEFAULT_POLL_MINUTES,
        help="install only: minutes between ticks; must divide 60.",
    )
    parser.add_argument(
        "--ping-output-file",
        type=Path,
        default=None,
        help="tick only: classify this recorded ping output instead of running claude "
        "(hand verification; see samples/); needs an explicit --state-dir.",
    )
    return parser


def _load_installed(
    state_dir: Path, err: TextIO
) -> tuple[PrimerConfig, PrimerState] | None:
    """The recorded config and state, or None after reporting why they are unusable."""
    try:
        config = load_config(state_dir)
        state = load_state(state_dir)
    except StoreError as exc:
        print(f"ERROR: {exc}", file=err)
        return None
    if config is None:
        print(
            f"ERROR: no {CONFIG_FILENAME} in {state_dir}; run --mode install first",
            file=err,
        )
        return None
    return config, state


def format_log_line(moment: datetime, outcome: PingOutcome) -> str:
    """One log line: when, which outcome, the window end it reported, and why.

    Every character that is not printable becomes a space, and the detail's whitespace
    then collapses to single spaces. A failed ping's detail can carry
    the result event's message, the CLI's stderr, a timeout note naming the last event
    the ping wrote, or several of these. The message and stderr are the CLI's own text
    and can span lines, while the log holds one line per ping attempt: its cap trims by
    line, so a split entry could lose its first half, and a terminal escape in that text
    would act on the terminal of whoever reads the log there. The collapsed detail is
    then cut to `LOG_DETAIL_MAX_CHARS`, so the line stays bounded whatever the CLI sent.
    """
    ends = to_utc_iso(outcome.resets_at) or "none"
    printable = "".join(c if c.isprintable() else " " for c in outcome.detail)
    detail = " ".join(printable.split())[:LOG_DETAIL_MAX_CHARS]
    return f"{to_utc_iso(moment)} {outcome.kind} window_ends_at={ends} {detail}"


def _read_ping_output_file(path: Path, err: TextIO) -> str | None:
    """The recorded ping output in *path*, or None after reporting why it is unreadable."""
    try:
        # Decoded with replacement, as run_process decodes a live ping, so a recording
        # classifies exactly as the output it stands in for would.
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        # The OSError text already ends with the path, so it is not repeated here.
        print(f"ERROR: --ping-output-file cannot be read: {exc}", file=err)
        return None


def _ping(
    config: PrimerConfig, state_dir: Path, recorded: str | None, seams: Seams
) -> PingOutcome:
    """The real ping's outcome, or the recorded output's when one was given."""
    if recorded is None:
        return run_ping(config, state_dir, runner=seams.run, base_env=seams.environ)
    return parse_ping_output(recorded)


def _alert_on_streak(
    before: PrimerState,
    after: PrimerState,
    outcome: PingOutcome,
    seams: BoundSeams,
) -> None:
    """Notify when this tick started or ended a failure streak; report a refused one.

    The warning goes to stderr, which launchd writes to launchd.err.log (task.err.log
    with no console, where win32's "not supported" warning lands). It never changes the
    tick's exit code: a machine with no notification route must still keep its window.
    """
    alert = alert_for(
        before.consecutive_failures, after.consecutive_failures, outcome.detail
    )
    if alert is None:
        return
    problem = send_alert(seams.run, alert, platform=seams.platform)
    if problem is not None:
        print(
            f"WARNING: could not show the alert notification: {problem}", file=seams.err
        )


def _refuse_unsafe_state_dir(state_dir: Path, seams: BoundSeams) -> bool:
    """Whether *state_dir* is unsafe to run from (`state_dir_guard`), after reporting why."""
    problem = state_dir_problem(state_dir, posix=POSIX_HOST, euid=seams.euid)
    if problem is not None:
        print(f"ERROR: {problem}", file=seams.err)
    return problem is not None


def _refuse_recorded_shim(config: PrimerConfig, seams: BoundSeams) -> bool:
    """Whether the recorded claude is a batch shim (`batch_shim_problem`), after saying so.

    Install refuses one, but a config.json recorded before it did may still name one,
    and every tick would then run it through cmd.exe.
    """
    problem = batch_shim_problem(config.claude_path, seams.platform)
    if problem is not None:
        print(f"ERROR: recorded in {CONFIG_FILENAME}: {problem}", file=seams.err)
    return problem is not None


def run_tick(args: argparse.Namespace, seams: BoundSeams) -> int:
    """One scheduled tick: ping once the recorded window has ended, then record it."""
    state_dir: Path = args.state_dir
    if args.ping_output_file is not None and not args.state_dir_given:
        # A hand verification must never classify its recording into the live record.
        print("ERROR: --ping-output-file needs an explicit --state-dir", file=seams.err)
        return EXIT_USAGE
    if _refuse_unsafe_state_dir(state_dir, seams):
        return EXIT_USAGE
    loaded = _load_installed(state_dir, seams.err)
    if loaded is None:
        return EXIT_USAGE
    config, state = loaded
    if _refuse_recorded_shim(config, seams):
        return EXIT_USAGE
    recorded: str | None = None
    if args.ping_output_file is not None:
        # Read while validating, before any decision: a file that cannot be read is a
        # bad argument (exit 2), never a failed ping (exit 1).
        recorded = _read_ping_output_file(args.ping_output_file, seams.err)
        if recorded is None:
            return EXIT_USAGE
    now = seams.now()
    if decide_tick(now, config.start, state.window_ends_at) is not TickAction.PING:
        return EXIT_OK
    outcome = trusted_window_end(_ping(config, state_dir, recorded, seams), now)
    new_state = apply_outcome(state, outcome, now)
    save_state(state_dir, new_state)
    append_log(state_dir, format_log_line(now, outcome))
    # Last, after the record: a notification that hangs to its timeout or fails must
    # never cost the state or the log line.
    _alert_on_streak(state, new_state, outcome, seams)
    if outcome.kind == OUTCOME_FAILED:
        return EXIT_FAILED
    return EXIT_OK


def run_status(args: argparse.Namespace, seams: BoundSeams) -> int:
    """Print what is configured and recorded, and whether the scheduler has the job.

    Exits 1 while a failure streak lasts, so a person or a monitor asking whether the
    job works gets the answer from the exit code, not only from reading the rows.
    """
    loaded = _load_installed(args.state_dir, seams.err)
    if loaded is None:
        return EXIT_USAGE
    config, state = loaded
    backend = _backend(seams)
    job = (backend.label, backend.describe_state())
    rows = status_rows(config, state, seams.now(), job)
    width = max(len(label) for label, _value in rows) + STATUS_LABEL_GAP
    for label, value in rows:
        print(f"{label:<{width}}{value}", file=seams.out)
    if is_failing(state.consecutive_failures):
        return EXIT_FAILED
    return EXIT_OK


def _optional_moment(text: str | None) -> datetime | None:
    """An operator-typed moment as UTC, or None; ValueError when it is not ISO 8601."""
    if text is None:
        return None
    return parse_user_datetime(text)


def _validate_install(
    args: argparse.Namespace, seams: BoundSeams
) -> tuple[datetime | None, datetime | None, str] | None:
    """(start, last run, claude path) when the settings are usable; None after reporting."""
    try:
        validate_poll_minutes(args.poll_minutes)
        start = _optional_moment(args.start)
        last_run = _optional_moment(args.last_run)
        validate_last_run(last_run, seams.now())
        validate_prompt(args.prompt)
        validate_model(args.model)
        # Absolute; refused when unreadable, a Store alias or a batch shim (install_checks).
        return start, last_run, locate_claude(seams.which, platform=seams.platform)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=seams.err)
        return None


def _backend(seams: Seams) -> SchedulerBackend:
    """The scheduler backend this platform uses, wired to the injected seams."""
    return select_backend(
        seams.platform, run=seams.run, home=seams.home, environ=seams.environ
    )


def run_install(args: argparse.Namespace, seams: BoundSeams) -> int:
    """Validate the settings, record them, then schedule the job and confirm it."""
    validated = _validate_install(args, seams)
    if validated is None:
        return EXIT_USAGE
    start, last_run, claude_path = validated
    state_dir: Path = args.state_dir
    try:
        state = load_state(state_dir)
    except StoreError as exc:
        print(f"ERROR: {exc}", file=seams.err)
        return EXIT_USAGE
    created = ensure_private_dir(state_dir)
    if created is not None:
        print(f"ERROR: {created}", file=seams.err)
        return EXIT_USAGE
    if _refuse_unsafe_state_dir(state_dir, seams):
        return EXIT_USAGE
    config = PrimerConfig(
        start, args.model, args.prompt, args.poll_minutes, claude_path
    )
    # Recorded before the job is scheduled: launchd's RunAtLoad fires a tick at
    # bootstrap, and that tick reads config.json and state.json. Do not reorder this to
    # record after the scheduler confirms the job, or that first tick would find no install.
    save_config(state_dir, config)
    if last_run is not None:
        save_state(state_dir, record_manual_last_run(state, last_run))
    backend = _backend(seams)
    request = InstallRequest(seams.python, seams.script, state_dir, args.poll_minutes)
    try:
        backend.install(request)
    except AgentError as exc:
        print(f"ERROR: {exc}", file=seams.err)
        return EXIT_FAILED
    print(
        f"Installed the {backend.label}: a tick every {args.poll_minutes} min, at wake "
        f"and at login; state in {state_dir}",
        file=seams.out,
    )
    return EXIT_OK


def run_uninstall(args: argparse.Namespace, seams: BoundSeams) -> int:
    """Remove the scheduled job and confirm it is gone; the state directory is kept."""
    backend = _backend(seams)
    try:
        backend.uninstall()
    except AgentError as exc:
        print(f"ERROR: {exc}", file=seams.err)
        return EXIT_FAILED
    print(
        f"Uninstalled the {backend.label}; state kept in {args.state_dir}",
        file=seams.out,
    )
    return EXIT_OK


_HANDLERS: dict[str, Callable[[argparse.Namespace, BoundSeams], int]] = {
    "install": run_install,
    "uninstall": run_uninstall,
    "tick": run_tick,
    "status": run_status,
}


def main(argv: list[str] | None = None, *, seams: Seams | None = None) -> int:
    """Parse the arguments and run the chosen mode. Returns the exit code."""
    # First statement: launchd writes this output to a file, where a locale-derived
    # encoding could fail on a non-ASCII path or error message.
    stdio_utf8.force_utf8_streams(sys.stdout, sys.stderr)
    if seams is None:
        seams = Seams()
    default_dir = default_state_dir(seams.home, seams.environ, seams.platform)
    parser = build_parser(default_dir)
    args = parser.parse_args(argv)
    args.state_dir_given = not isinstance(args.state_dir, DefaultStateDir)
    chosen: Path = args.state_dir if args.state_dir_given else args.state_dir.path
    problem = state_dir_text_problem(str(chosen))
    if problem is not None:
        parser.error(problem)
    # Resolved once, here: the job the scheduler runs must name an absolute directory,
    # and every mode must agree on which directory a relative --state-dir meant.
    args.state_dir = chosen.expanduser().resolve()
    # With no console (pythonw.exe) the streams are None, and every ERROR line would be
    # lost: each missing one becomes a log file in the state directory instead.
    with missing_streams_opened(seams.out, seams.err, args.state_dir) as (out, err):
        return _HANDLERS[args.mode](args, bind_streams(seams, out, err))


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
