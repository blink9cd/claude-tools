"""The usage_window_primer tool's files under its state directory: config, state, and the log.

`config.json` is written only by ``--mode install``; `state.json` by ``--mode tick`` and
by an install given ``--last-run``. Both are replaced atomically -- written to a temporary
file in the same directory, then renamed over the original -- because ``--mode status``
may read either while a tick is writing it. A file that exists but does not hold what
this tool wrote raises `StoreError` when read. ``--mode tick`` and ``--mode status``
never overwrite a corrupt file of either kind. ``--mode install`` is deliberately
asymmetric: it reads
`state.json` and refuses a corrupt one, preserving the window evidence, but replaces
`config.json` without reading it.
"""

from __future__ import annotations

import contextlib
import json
import os
import reprlib
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

CONFIG_FILENAME = "config.json"
STATE_FILENAME = "state.json"
LOG_FILENAME = "usage_window_primer.log"
# Caps the log so a long offline stretch -- one failed ping per tick -- cannot grow it
# without bound; at the default 5-minute tick this is about three and a half days.
LOG_MAX_LINES = 1000
# The longest quote of a malformed value an error carries. A tick reports a corrupt file
# on every run, so a huge field must not become a huge line in the error log each time.
QUOTED_VALUE_MAX_CHARS = 80
_QUOTE = reprlib.Repr(
    maxlevel=2,
    maxstring=QUOTED_VALUE_MAX_CHARS,
    maxother=QUOTED_VALUE_MAX_CHARS,
    maxlong=QUOTED_VALUE_MAX_CHARS,
)
# Unicode's control characters (C0, DEL and C1) and its surrogates. A control character
# cannot travel as a process argument (NUL) or into a plist; a lone surrogate, which is
# how an undecodable byte arrives, cannot be encoded at all.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cs"})


class StoreError(Exception):
    """A config or state file exists but does not hold what this tool wrote."""


def has_unsafe_character(text: str) -> bool:
    """Whether *text* holds a control character or a lone surrogate."""
    return any(
        unicodedata.category(character) in _UNSAFE_CATEGORIES for character in text
    )


def quote_bounded(value: object) -> str:
    """*value*'s repr for an error line, cut to about `QUOTED_VALUE_MAX_CHARS`."""
    return _QUOTE.repr(value)


@dataclass(frozen=True)
class PrimerConfig:
    """What ``--mode install`` recorded."""

    start: datetime | None
    model: str
    prompt: str
    poll_minutes: int
    claude_path: str


@dataclass(frozen=True)
class PrimerState:
    """What the ticks have established about the current window.

    ``last_ping_at`` is the last ping that did not fail, or the one ``--last-run``
    reported; ``last_attempt_at`` is the last ping this tool sent, whatever its outcome.
    The attempt comes last and defaults to None so every positional construction, and a
    state.json written before it existed, still works.
    """

    window_ends_at: datetime | None
    window_end_source: str | None
    last_ping_at: datetime | None
    last_outcome: str | None
    consecutive_failures: int
    last_attempt_at: datetime | None = None


EMPTY_STATE = PrimerState(None, None, None, None, 0)


def to_utc_iso(moment: datetime | None) -> str | None:
    """*moment* as a UTC ISO 8601 string, or None."""
    if moment is None:
        return None
    return moment.astimezone(UTC).isoformat()


def _from_utc_iso(value: object, source: Path) -> datetime | None:
    """A stored timestamp as an aware datetime; `StoreError` naming *source* otherwise."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise StoreError(
            f"{source}: expected a timestamp string, found {quote_bounded(value)}"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        # Not the ValueError's own text: it quotes the whole value.
        raise StoreError(
            f"{source}: not an ISO 8601 timestamp: {quote_bounded(value)}"
        ) from exc
    if parsed.tzinfo is None:
        raise StoreError(
            f"{source}: timestamp {quote_bounded(value)} carries no UTC offset"
        )
    return parsed


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Replace *path* with *data* so that no reader ever sees a partial file.

    A failed write removes its temporary file before re-raising: a fault that repeats on
    every unattended tick (a full disk) must not pile stray files up in the directory.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            # Closed before the rename, and before any cleanup below: some platforms
            # refuse to rename or unlink a file that is still open.
            handle.close()
            os.replace(temporary, path)
        except BaseException:
            # After a failed write or flush, close() flushes the stuck buffer again and
            # fails the same way. That repeat carries nothing new, so it is suppressed:
            # it must neither skip the unlink nor replace the original error re-raised.
            with contextlib.suppress(OSError):
                handle.close()
            temporary.unlink(missing_ok=True)
            raise


def _write_json(path: Path, payload: dict[str, object]) -> None:
    """Atomically write *payload* as indented, key-sorted JSON."""
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    write_bytes_atomic(path, text.encode("utf-8"))


def _read_json_object(path: Path) -> dict[str, object] | None:
    """The JSON object in *path*; None when the file is absent; `StoreError` otherwise."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise StoreError(f"{path}: {exc}") from exc
    try:
        value = json.loads(raw)
    # RecursionError: a file nested deeper than the decoder can descend is corrupt too,
    # and must be reported rather than crash the tick with a traceback.
    except (ValueError, RecursionError) as exc:
        raise StoreError(f"{path}: not valid JSON ({exc})") from exc
    if not isinstance(value, dict):
        raise StoreError(f"{path}: expected a JSON object")
    return value


def _require_str(payload: dict[str, object], key: str, source: Path) -> str:
    """``payload[key]`` as a string; `StoreError` when it is anything else."""
    value = payload.get(key)
    if not isinstance(value, str):
        raise StoreError(
            f"{source}: {key!r} must be a string, found {quote_bounded(value)}"
        )
    return value


def _require_safe_str(payload: dict[str, object], key: str, source: Path) -> str:
    """``payload[key]`` as a string the ping can pass as an argument; `StoreError` otherwise.

    A NUL cannot travel in argv at all, and the other control characters and lone
    surrogates would fail in the subprocess layer or the log: either way an uncaught
    error on every tick, where this reports the file once per tick as corrupt.
    """
    value = _require_str(payload, key, source)
    if has_unsafe_character(value):
        raise StoreError(
            f"{source}: {key!r} holds a control character or a lone surrogate: "
            f"{quote_bounded(value)}"
        )
    return value


def _optional_str(payload: dict[str, object], key: str, source: Path) -> str | None:
    """``payload[key]`` as a string or None; `StoreError` when it is anything else."""
    if payload.get(key) is None:
        return None
    return _require_str(payload, key, source)


def _require_int(payload: dict[str, object], key: str, source: Path) -> int:
    """``payload[key]`` as an integer (never a bool); `StoreError` otherwise."""
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise StoreError(
            f"{source}: {key!r} must be an integer, found {quote_bounded(value)}"
        )
    return value


def save_config(state_dir: Path, config: PrimerConfig) -> None:
    """Atomically record *config* in the state directory."""
    payload: dict[str, object] = {
        "start": to_utc_iso(config.start),
        "model": config.model,
        "prompt": config.prompt,
        "poll_minutes": config.poll_minutes,
        "claude_path": config.claude_path,
    }
    _write_json(state_dir / CONFIG_FILENAME, payload)


def load_config(state_dir: Path) -> PrimerConfig | None:
    """The recorded config; None when no install has run; `StoreError` when corrupt."""
    path = state_dir / CONFIG_FILENAME
    payload = _read_json_object(path)
    if payload is None:
        return None
    return PrimerConfig(
        start=_from_utc_iso(payload.get("start"), path),
        model=_require_safe_str(payload, "model", path),
        prompt=_require_safe_str(payload, "prompt", path),
        poll_minutes=_require_int(payload, "poll_minutes", path),
        claude_path=_require_safe_str(payload, "claude_path", path),
    )


def save_state(state_dir: Path, state: PrimerState) -> None:
    """Atomically record *state* in the state directory."""
    payload: dict[str, object] = {
        "window_ends_at": to_utc_iso(state.window_ends_at),
        "window_end_source": state.window_end_source,
        "last_ping_at": to_utc_iso(state.last_ping_at),
        "last_outcome": state.last_outcome,
        "consecutive_failures": state.consecutive_failures,
        "last_attempt_at": to_utc_iso(state.last_attempt_at),
    }
    _write_json(state_dir / STATE_FILENAME, payload)


def load_state(state_dir: Path) -> PrimerState:
    """The recorded state; `EMPTY_STATE` before any tick; `StoreError` when corrupt."""
    path = state_dir / STATE_FILENAME
    payload = _read_json_object(path)
    if payload is None:
        return EMPTY_STATE
    return PrimerState(
        window_ends_at=_from_utc_iso(payload.get("window_ends_at"), path),
        window_end_source=_optional_str(payload, "window_end_source", path),
        last_ping_at=_from_utc_iso(payload.get("last_ping_at"), path),
        last_outcome=_optional_str(payload, "last_outcome", path),
        consecutive_failures=_require_int(payload, "consecutive_failures", path),
        # Optional on read: a state.json written before this key existed lacks it.
        last_attempt_at=_from_utc_iso(payload.get("last_attempt_at"), path),
    )


def append_log(state_dir: Path, line: str, max_lines: int = LOG_MAX_LINES) -> None:
    """Append *line* to the log, keeping only the newest *max_lines* lines."""
    path = state_dir / LOG_FILENAME
    try:
        existing = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        existing = []
    kept = [*existing, line][-max_lines:]
    # Replacement, as on the read above: a log line quotes untrusted CLI text, whose JSON
    # escapes can decode to a lone surrogate that strict UTF-8 cannot encode.
    text = "\n".join(kept) + "\n"
    write_bytes_atomic(path, text.encode("utf-8", errors="replace"))
