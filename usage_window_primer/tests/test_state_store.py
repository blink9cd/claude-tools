"""The state directory's files: round trips, atomic replacement, corruption, the log cap."""

from __future__ import annotations

import errno
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TOOL_ROOT / "src"))

from state_store import (  # noqa: E402 - import follows the sys.path bootstrap above
    CONFIG_FILENAME,
    EMPTY_STATE,
    LOG_FILENAME,
    STATE_FILENAME,
    PrimerConfig,
    PrimerState,
    StoreError,
    append_log,
    load_config,
    load_state,
    save_config,
    save_state,
    to_utc_iso,
    write_bytes_atomic,
)

_START = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
_CONFIG = PrimerConfig(
    _START, "haiku", "Reply with the single word: ok", 5, "/opt/fake/bin/claude"
)
# Far deeper than the JSON decoder's C stack-overflow check lets it descend.
_NESTING_PAST_THE_RECURSION_LIMIT = 200_000
# A field far longer than any error line should quote, and the most an error about it
# may add to the file's path: the key, the explanation and a truncated quote.
_HUGE_VALUE_CHARS = 100_000
_BOUNDED_MESSAGE_CHARS = 200


class _FullDiskFileIO(io.FileIO):
    """A real file on a disk with no space left: every write fails, numbered by attempt."""

    def __init__(self, name: str) -> None:
        super().__init__(name, "wb")
        self.attempts = 0

    def write(self, _data: object, /) -> int:
        self.attempts += 1
        raise OSError(errno.ENOSPC, f"no space left (write attempt {self.attempts})")


def _open_on_a_full_disk(*, dir: Path, prefix: str, delete: bool) -> io.BufferedWriter:
    """Stand in for NamedTemporaryFile with a real file whose buffered writes never flush.

    A small write only fills the buffer and succeeds; the flush fails, and so does the
    second flush that close() performs: the sequence a genuinely full disk produces.
    *delete* is accepted to match the real call and ignored: this file is never removed
    automatically either.
    """
    descriptor, name = tempfile.mkstemp(dir=dir, prefix=prefix)
    os.close(descriptor)
    return io.BufferedWriter(_FullDiskFileIO(name))


def _failing_call(error_number: int, message: str) -> mock.Mock:
    """A stand-in for an os function that always fails with this OSError."""
    return mock.Mock(side_effect=OSError(error_number, message))


def _valid_config_payload() -> dict[str, object]:
    return {
        "start": "2026-01-01T06:00:00+00:00",
        "model": "haiku",
        "prompt": "hi",
        "poll_minutes": 5,
        "claude_path": "/opt/fake/bin/claude",
    }


def _valid_state_payload() -> dict[str, object]:
    return {
        "window_ends_at": "2026-01-01T18:00:00+00:00",
        "window_end_source": "server",
        "last_ping_at": "2026-01-01T13:00:00+00:00",
        "last_outcome": "ok",
        "consecutive_failures": 0,
    }


class StateStoreTest(unittest.TestCase):
    """Everything under the state directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_config_round_trips(self) -> None:
        for start in (_START, None):
            with self.subTest(start=start):
                config = PrimerConfig(start, "haiku", "hi", 15, "/opt/fake/bin/claude")
                save_config(self.state_dir, config)
                self.assertEqual(load_config(self.state_dir), config)

    def test_missing_config_loads_as_none(self) -> None:
        self.assertIsNone(load_config(self.state_dir))

    def test_state_round_trips(self) -> None:
        # The attempt is later than the last success: a failed ping records only the one.
        populated = PrimerState(
            _START + timedelta(hours=5),
            "server",
            _START,
            "failed",
            2,
            _START + timedelta(hours=6),
        )
        # EMPTY_STATE is what a saved state holds before any ping: every optional null.
        for state in (populated, EMPTY_STATE):
            with self.subTest(state=state):
                save_state(self.state_dir, state)
                self.assertEqual(load_state(self.state_dir), state)

    def test_missing_state_loads_as_empty(self) -> None:
        self.assertEqual(load_state(self.state_dir), EMPTY_STATE)

    def test_a_state_file_from_before_last_attempt_at_still_loads(self) -> None:
        # Exactly the keys the installed agent wrote before last_attempt_at existed: a
        # tick running the new code must keep reading the file it finds on disk.
        payload = _valid_state_payload()
        self.assertNotIn("last_attempt_at", payload)
        (self.state_dir / STATE_FILENAME).write_text(
            json.dumps(payload), encoding="utf-8", newline="\n"
        )
        self.assertEqual(
            load_state(self.state_dir),
            PrimerState(
                datetime(2026, 1, 1, 18, 0, tzinfo=UTC),
                "server",
                datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
                "ok",
                0,
                None,
            ),
        )

    def test_saving_leaves_no_temporary_files(self) -> None:
        save_config(self.state_dir, _CONFIG)
        save_state(self.state_dir, EMPTY_STATE)
        self.assertEqual(
            sorted(os.listdir(self.state_dir)), [CONFIG_FILENAME, STATE_FILENAME]
        )
        # A save that fails anywhere between creating its temporary file and the rename
        # must remove that file (an unattended tick would otherwise strand one per
        # failing run), and must raise the failure that happened, not the repeat of it
        # that closing a file with an unflushable buffer produces.
        cases: dict[str, tuple[object, str, object, tuple[int, str]]] = {
            "flush on a full disk": (
                tempfile,
                "NamedTemporaryFile",
                _open_on_a_full_disk,
                (errno.ENOSPC, "no space left (write attempt 1)"),
            ),
            "fsync": (
                os,
                "fsync",
                _failing_call(errno.EIO, "fsync failed"),
                (errno.EIO, "fsync failed"),
            ),
            "replace": (
                os,
                "replace",
                _failing_call(errno.EXDEV, "replace failed"),
                (errno.EXDEV, "replace failed"),
            ),
        }
        for name, (module, attribute, stand_in, expected_args) in cases.items():
            with self.subTest(failure=name):
                failing_dir = self.state_dir / name.replace(" ", "-")
                failing_dir.mkdir()
                with (
                    mock.patch.object(module, attribute, stand_in),
                    self.assertRaises(OSError) as caught,
                ):
                    save_state(failing_dir, EMPTY_STATE)
                # One comparison, so a regression shows both the leak and the wrong error.
                self.assertEqual(
                    (os.listdir(failing_dir), caught.exception.args),
                    ([], expected_args),
                )

    def test_write_bytes_atomic_creates_missing_directories(self) -> None:
        target = self.state_dir / "a" / "b" / "file.bin"
        write_bytes_atomic(target, b"payload")
        self.assertEqual(target.read_bytes(), b"payload")

    def test_timestamps_are_stored_as_utc(self) -> None:
        moment = datetime(2026, 1, 1, 11, 0, tzinfo=timezone(timedelta(hours=5)))
        self.assertEqual(to_utc_iso(moment), "2026-01-01T06:00:00+00:00")
        self.assertIsNone(to_utc_iso(None))

    def test_unreadable_files_raise_store_error_naming_the_file(self) -> None:
        path = self.state_dir / CONFIG_FILENAME
        cases: dict[str, bytes | None] = {
            "not json": b"{",
            "a json array": b"[]",
            "invalid utf-8": b"\xff\xfe{",
            "nested past the recursion limit": b"[" * _NESTING_PAST_THE_RECURSION_LIMIT,
            "a directory": None,
        }
        for name, content in cases.items():
            with self.subTest(case=name):
                if content is None:
                    # The last case: replace the previous case's file with a directory.
                    path.unlink(missing_ok=True)
                    path.mkdir()
                else:
                    path.write_bytes(content)
                with self.assertRaises(StoreError) as caught:
                    load_config(self.state_dir)
                self.assertIn(CONFIG_FILENAME, str(caught.exception))

    def test_malformed_config_fields_raise_store_error(self) -> None:
        cases: dict[str, tuple[str, object]] = {
            "model not a string": ("model", 3),
            "poll_minutes a bool": ("poll_minutes", True),
            "poll_minutes a string": ("poll_minutes", "5"),
            "start not a string": ("start", 1767225600),
            "start not iso": ("start", "tomorrow"),
            "start without an offset": ("start", "2026-01-01T06:00:00"),
            "claude_path missing": ("claude_path", None),
        }
        for name, (key, value) in cases.items():
            with self.subTest(case=name):
                payload = _valid_config_payload()
                payload[key] = value
                (self.state_dir / CONFIG_FILENAME).write_text(
                    json.dumps(payload), encoding="utf-8", newline="\n"
                )
                with self.assertRaises(StoreError):
                    load_config(self.state_dir)

    def test_config_text_with_a_control_character_raises_store_error(self) -> None:
        # Regression: a NUL in claude_path, prompt or model loaded fine and then raised
        # ValueError from the subprocess layer, an uncaught traceback on every tick.
        # A lone surrogate fails the same way, as an encoding error. Built with chr(): an
        # editing tool can decode a typed escape into the raw character.
        for key in ("prompt", "model", "claude_path"):
            for character in (chr(0), chr(1), "\n", chr(0x7F), chr(0x85), chr(0xD800)):
                with self.subTest(key=key, character=hex(ord(character))):
                    payload = _valid_config_payload()
                    payload[key] = f"a{character}b"
                    (self.state_dir / CONFIG_FILENAME).write_text(
                        json.dumps(payload), encoding="utf-8", newline="\n"
                    )
                    with self.assertRaisesRegex(StoreError, "control character"):
                        load_config(self.state_dir)

    def test_a_huge_malformed_value_makes_a_bounded_error(self) -> None:
        # Regression: the error quoted the whole value, so one huge field wrote a huge
        # line to the error log on every tick until someone fixed the file.
        cases: dict[str, tuple[str, object]] = {
            "a list for a string": ("model", ["x" * _HUGE_VALUE_CHARS]),
            "a string for an integer": ("poll_minutes", "9" * _HUGE_VALUE_CHARS),
            "a timestamp that is not iso": ("start", "y" * _HUGE_VALUE_CHARS),
        }
        for name, (key, value) in cases.items():
            with self.subTest(case=name):
                payload = _valid_config_payload()
                payload[key] = value
                path = self.state_dir / CONFIG_FILENAME
                path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
                with self.assertRaises(StoreError) as caught:
                    load_config(self.state_dir)
                message = str(caught.exception)
                self.assertLess(len(message), len(str(path)) + _BOUNDED_MESSAGE_CHARS)

    def test_malformed_state_fields_raise_store_error(self) -> None:
        cases: dict[str, tuple[str, object]] = {
            "source not a string": ("window_end_source", 1),
            "failures missing": ("consecutive_failures", None),
            "last ping not iso": ("last_ping_at", "yesterday"),
            "last attempt not a string": ("last_attempt_at", 1767225600),
        }
        for name, (key, value) in cases.items():
            with self.subTest(case=name):
                payload = _valid_state_payload()
                payload[key] = value
                (self.state_dir / STATE_FILENAME).write_text(
                    json.dumps(payload), encoding="utf-8", newline="\n"
                )
                with self.assertRaises(StoreError):
                    load_state(self.state_dir)

    def test_a_corrupt_file_is_left_untouched(self) -> None:
        path = self.state_dir / STATE_FILENAME
        path.write_bytes(b"{broken")
        with self.assertRaises(StoreError):
            load_state(self.state_dir)
        self.assertEqual(path.read_bytes(), b"{broken")

    def test_append_log_creates_then_appends(self) -> None:
        append_log(self.state_dir, "first")
        append_log(self.state_dir, "second")
        text = (self.state_dir / LOG_FILENAME).read_text(encoding="utf-8")
        self.assertEqual(text, "first\nsecond\n")

    def test_append_log_keeps_only_the_newest_lines(self) -> None:
        for number in range(5):
            append_log(self.state_dir, f"line {number}", max_lines=3)
        text = (self.state_dir / LOG_FILENAME).read_text(encoding="utf-8")
        self.assertEqual(text.splitlines(), ["line 2", "line 3", "line 4"])

    def test_append_log_replaces_a_lone_surrogate(self) -> None:
        # A log line quotes the CLI's own text, where a JSON escape can decode to a lone
        # surrogate that UTF-8 cannot encode. Built with chr(): a typed escape can be
        # decoded into the raw, unsavable character by an editing tool.
        append_log(self.state_dir, f"a{chr(0xD800)}b")
        data = (self.state_dir / LOG_FILENAME).read_bytes()
        self.assertEqual(data.decode("utf-8"), "a?b\n")


if __name__ == "__main__":
    unittest.main()
