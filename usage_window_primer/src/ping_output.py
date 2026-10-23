"""Classify the stream-json output of one headless `claude -p` ping.

The output is one JSON object per line from an external process whose format this
project does not control, so `parse_ping_output` treats every line as untrusted and never
raises. Two events matter, both measured on a real ping:

* ``rate_limit_event`` -- its ``rate_limit_info.unifiedWindows.five_hour.resetsAt``
  (epoch seconds) is the authoritative end of the current 5-hour window, and its
  ``status`` is ``rejected`` when the account is at its limit;
* ``result`` -- the call succeeded only when its ``subtype`` is ``success`` and its
  ``is_error`` is false. An error result can keep subtype ``success`` -- an API error such
  as an expired login does -- so a failure's detail quotes the event's own ``result`` text
  when it carries any, rather than naming the subtype alone.

A ping killed at its timeout never reaches its ``result``; `describe_last_event` names the
last event it did write, so the log shows how far the CLI got. It never raises either.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

OUTCOME_OK = "ok"
OUTCOME_LIMITED = "limited"
OUTCOME_FAILED = "failed"
# The detail of a stream that held no result event. Public so a caller can tell a
# result's own reason, which is worth logging beside stderr, from this absence.
NO_RESULT_DETAIL = "no result event in the ping output"

_REJECTED_STATUS = "rejected"
_SUCCESS_SUBTYPE = "success"
_FIVE_HOUR = "five_hour"
# The longest event label `describe_last_event` returns: room for any measured
# ``type/subtype`` pair, while a junk type cannot crowd the rest of a log line out.
_EVENT_LABEL_MAX_CHARS = 60


@dataclass(frozen=True)
class PingOutcome:
    """What one ping established: its kind, the window end it reported, and why."""

    kind: str
    resets_at: datetime | None
    detail: str


def _json_objects(text: str) -> list[dict[str, object]]:
    """Every line of *text* that parses as a JSON object; anything else is skipped."""
    objects: list[dict[str, object]] = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def _dict_at(container: dict[str, object], key: str) -> dict[str, object]:
    """``container[key]`` when it is a JSON object, else an empty dict."""
    value = container.get(key)
    if isinstance(value, dict):
        return value
    return {}


def _epoch_to_utc(value: object) -> datetime | None:
    """*value* as a UTC datetime when it is a representable epoch number, else None."""
    # bool is an int subclass: JSON true must not read as one second past 1970.
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _window_end(info: dict[str, object]) -> datetime | None:
    """The five-hour window end one ``rate_limit_info`` carries, preferring unifiedWindows."""
    unified = _dict_at(_dict_at(info, "unifiedWindows"), _FIVE_HOUR)
    if "resetsAt" in unified:
        return _epoch_to_utc(unified["resetsAt"])
    if info.get("rateLimitType") == _FIVE_HOUR:
        return _epoch_to_utc(info.get("resetsAt"))
    return None


def _result_message(result: dict[str, object]) -> str | None:
    """The CLI's own text in a result event, or None when it carries none."""
    message = result.get("result")
    if isinstance(message, str) and message.strip():
        return message
    return None


def _failed_result_detail(result: dict[str, object]) -> str:
    """Why a result event that is not a success failed, in the CLI's own words when it gave any.

    An API error -- an expired login among them -- arrives with ``subtype`` still ``success``
    and only ``is_error`` set, so naming the subtype alone would log "success" for a failure.
    """
    subtype = result.get("subtype")
    message = _result_message(result)
    if subtype == _SUCCESS_SUBTYPE:
        return (
            f"CLI error: {message}" if message else "CLI error result with no message"
        )
    if message:
        return f"result subtype {subtype!r}: {message}"
    return f"result subtype {subtype!r}"


def _classify(
    status: object, resets_at: datetime | None, result: dict[str, object] | None
) -> PingOutcome:
    """The outcome for the facts one stream established."""
    if status == _REJECTED_STATUS and resets_at is not None:
        return PingOutcome(OUTCOME_LIMITED, resets_at, "at the usage limit")
    if result is None:
        return PingOutcome(OUTCOME_FAILED, None, NO_RESULT_DETAIL)
    if result.get("subtype") != _SUCCESS_SUBTYPE or result.get("is_error") is not False:
        return PingOutcome(OUTCOME_FAILED, None, _failed_result_detail(result))
    return PingOutcome(OUTCOME_OK, resets_at, "ping succeeded")


# fuzz-target
def parse_ping_output(text: str) -> PingOutcome:
    """Classify a ping's stream-json *text*. Never raises, whatever *text* holds."""
    status: object = None
    resets_at: datetime | None = None
    result: dict[str, object] | None = None
    for event in _json_objects(text):
        kind = event.get("type")
        if kind == "rate_limit_event":
            info = _dict_at(event, "rate_limit_info")
            status = info.get("status")
            resets_at = _window_end(info)
        elif kind == "result":
            result = event
    return _classify(status, resets_at, result)


# fuzz-target
def describe_last_event(text: str) -> str:
    """The last stream event in *text* as ``type/subtype``, for a ping that never finished.

    Never raises: *text* is whatever a killed child had written.
    """
    if not text.strip():
        return "no output"
    events = _json_objects(text)
    if not events:
        return "no complete event"
    last = events[-1]
    kind = last.get("type")
    label = kind if isinstance(kind, str) else "?"
    subtype = last.get("subtype")
    if isinstance(subtype, str):
        label = f"{label}/{subtype}"
    return label[:_EVENT_LABEL_MAX_CHARS] or "?"
