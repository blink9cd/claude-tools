"""What ``--mode status`` prints: one (label, value) row per recorded fact, in local time.

Pure functions of the recorded config and state, the moment, and the scheduler's own
answer, so every row is tested without a clock, a file or a scheduler.
"""

from __future__ import annotations

from datetime import datetime

from failure_alert import is_failing
from ping_output import OUTCOME_FAILED
from state_store import PrimerConfig, PrimerState
from window_rules import next_ping_due


def format_local(moment: datetime) -> str:
    """*moment* in this machine's local time, to the minute."""
    return moment.astimezone().isoformat(timespec="minutes")


def _local_or(moment: datetime | None, missing: str) -> str:
    """*moment* in local time, or *missing* when there is none."""
    if moment is None:
        return missing
    return format_local(moment)


def _attempt_text(state: PrimerState) -> str:
    """When the last ping attempt happened, as status prints it.

    A state.json written before last_attempt_at existed recorded only successful pings, so its
    last_ping_at is the last attempt exactly when the last outcome was not a failure.
    """
    if state.last_attempt_at is not None:
        return format_local(state.last_attempt_at)
    if state.last_outcome is None:
        return "never"
    if state.last_outcome == OUTCOME_FAILED:
        return "unknown"
    return _local_or(state.last_ping_at, "never")


def _next_due_text(due: datetime | None, now: datetime) -> str:
    """When the next ping is due, as status prints it."""
    if due is None or due <= now:
        return "at the next tick"
    return format_local(due)


def _health_text(consecutive_failures: int) -> str:
    """Whether the pings are getting through, as status prints it."""
    if is_failing(consecutive_failures):
        return f"failing ({consecutive_failures} pings in a row)"
    return "ok"


def status_rows(
    config: PrimerConfig, state: PrimerState, now: datetime, job: tuple[str, str]
) -> list[tuple[str, str]]:
    """The (label, value) rows ``--mode status`` prints; *job* is (label, state)."""
    due = next_ping_due(config.start, state.window_ends_at)
    window_ends = _local_or(state.window_ends_at, "unknown")
    return [
        ("start", _local_or(config.start, "not set")),
        # The outcome sits beside the attempt it belongs to, never beside the last
        # success: a failed ping records no success time, so that pairing dated a
        # failure by whatever ping last worked, possibly days earlier.
        ("last attempt", f"{_attempt_text(state)} ({state.last_outcome or 'none'})"),
        ("last success", _local_or(state.last_ping_at, "never")),
        ("window ends", f"{window_ends} ({state.window_end_source or 'none'})"),
        ("next ping due", _next_due_text(due, now)),
        ("consecutive failures", str(state.consecutive_failures)),
        (f"scheduler ({job[0]})", job[1]),
        ("health", _health_text(state.consecutive_failures)),
    ]
