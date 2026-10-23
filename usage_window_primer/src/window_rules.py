"""The usage_window_primer tick's decisions, as pure functions of time and recorded facts.

Nothing here reads a clock, a file or a process: the caller passes ``now`` in, which is
what lets every boundary -- a tick landing exactly on the window end -- be tested exactly.
A 5-hour window opens on the first message after the previous one ended, so a tick pings
as soon as the recorded end has passed and otherwise does nothing.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from ping_output import OUTCOME_FAILED, PingOutcome
from state_store import PrimerState

WINDOW_LENGTH = timedelta(hours=5)
# How far past a full window from the ping a reported end is still believed. A window
# that opens with the ping ends WINDOW_LENGTH later by the server's clock, and this
# machine's clock may run behind the server's. Fifteen minutes covers an unsynchronized
# clock's ordinary drift while staying far short of a second window.
WINDOW_END_SLACK = timedelta(minutes=15)

_SECONDS_PER_MINUTE = 60
_MINUTES_PER_HOUR = 60

SOURCE_SERVER = "server"
SOURCE_ESTIMATED = "estimated"
SOURCE_MANUAL = "manual"


class TickAction(StrEnum):
    """What one tick should do."""

    WAIT_FOR_START = "wait-for-start"
    PING = "ping"
    WAIT_FOR_WINDOW_END = "wait-for-window-end"


def decide_tick(
    now: datetime, start: datetime | None, window_ends_at: datetime | None
) -> TickAction:
    """Whether a tick at *now* should ping."""
    if start is not None and now < start:
        return TickAction.WAIT_FOR_START
    if window_ends_at is None or now >= window_ends_at:
        return TickAction.PING
    return TickAction.WAIT_FOR_WINDOW_END


def duration_text(delta: timedelta) -> str:
    """*delta* in whole hours and minutes, each shown only when present: ``5 h 15 min``."""
    total_minutes = int(delta.total_seconds()) // _SECONDS_PER_MINUTE
    hours, minutes = divmod(total_minutes, _MINUTES_PER_HOUR)
    parts = [f"{hours} h"] if hours else []
    if minutes or not parts:
        parts.append(f"{minutes} min")
    return " ".join(parts)


# fuzz-target
def trusted_window_end(outcome: PingOutcome, pinged_at: datetime) -> PingOutcome:
    """*outcome*, minus a reported window end no real window could have, saying why.

    A window running at *pinged_at* ends after it and at most `WINDOW_LENGTH` later, give
    or take `WINDOW_END_SLACK`. An end outside ``(pinged_at, pinged_at + WINDOW_LENGTH +
    WINDOW_END_SLACK]`` is dropped, so `apply_outcome` records the estimate instead: an
    end in the past would have every tick ping again, and one far ahead would stop the
    pings until then (a reported year-9999 end once froze the tool). The detail names
    the end that was refused, so the log shows why the source reads ``estimated``.
    """
    reported = outcome.resets_at
    latest = pinged_at + WINDOW_LENGTH + WINDOW_END_SLACK
    if reported is None or pinged_at < reported <= latest:
        return outcome
    bound = (
        f"(ping time, ping time + {duration_text(WINDOW_LENGTH)} + "
        f"{duration_text(WINDOW_END_SLACK)}]"
    )
    detail = (
        f"{outcome.detail}; server window end {reported.isoformat()} is outside "
        f"{bound}, estimated instead"
    )
    return replace(outcome, resets_at=None, detail=detail)


def apply_outcome(
    state: PrimerState, outcome: PingOutcome, pinged_at: datetime
) -> PrimerState:
    """The state after a ping sent at *pinged_at* produced *outcome*.

    Every outcome records *pinged_at* as the last attempt; only one that did not fail
    records it as the last successful ping too.
    """
    if outcome.kind == OUTCOME_FAILED:
        # The window end is left alone, so the very next tick retries the ping.
        return replace(
            state,
            last_outcome=outcome.kind,
            consecutive_failures=state.consecutive_failures + 1,
            last_attempt_at=pinged_at,
        )
    if outcome.resets_at is not None:
        ends_at, source = outcome.resets_at, SOURCE_SERVER
    else:
        ends_at, source = pinged_at + WINDOW_LENGTH, SOURCE_ESTIMATED
    return PrimerState(
        window_ends_at=ends_at,
        window_end_source=source,
        last_ping_at=pinged_at,
        last_outcome=outcome.kind,
        consecutive_failures=0,
        last_attempt_at=pinged_at,
    )


def record_manual_last_run(state: PrimerState, last_run: datetime) -> PrimerState:
    """The state after the operator reports a ping at *last_run* via ``--last-run``.

    The last attempt is left alone: this tool did not send that ping.
    """
    return replace(
        state,
        window_ends_at=last_run + WINDOW_LENGTH,
        window_end_source=SOURCE_MANUAL,
        last_ping_at=last_run,
    )


def next_ping_due(
    start: datetime | None, window_ends_at: datetime | None
) -> datetime | None:
    """The earliest moment a tick would ping.

    None when nothing is recorded; a moment at or before now also means the next tick
    pings.
    """
    candidates = [moment for moment in (start, window_ends_at) if moment is not None]
    if not candidates:
        return None
    return max(candidates)


def parse_user_datetime(text: str) -> datetime:
    """An operator-typed ISO 8601 moment as UTC; one without an offset is local time.

    Raises ValueError when *text* is not ISO 8601 or lies outside the supported range.
    """
    # astimezone() on a NAIVE datetime reads it as system local time, applying the DST
    # rule in force on that date -- which an offset captured "now" would get wrong.
    try:
        return datetime.fromisoformat(text).astimezone(UTC)
    except OverflowError as exc:
        raise ValueError(f"{text!r} is outside the supported date range") from exc
