"""Somewhere for output to go when the process has no console.

Under ``pythonw.exe`` -- how a Windows scheduled task runs without flashing a console --
``sys.stdout`` and ``sys.stderr`` are None, so every ``ERROR:`` and ``WARNING:`` line a
tick writes would be lost. Each missing stream is replaced by an append-mode log file in
the state directory: ``task.out.log`` and ``task.err.log``, the twins of the
``launchd.out.log`` and ``launchd.err.log`` launchd captures on macOS. A stream that
exists is used as it is, so a run from a terminal or under launchd is unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

OUT_LOG_NAME = "task.out.log"
ERR_LOG_NAME = "task.err.log"
LOG_ENCODING = "utf-8"
# A log line must never be the thing that fails: an unencodable character (a lone
# surrogate from a filename) is escaped rather than raised.
LOG_ERRORS = "backslashreplace"
# LF on every host, as usage_window_primer.log is written: the bytes do not depend on the machine.
LOG_NEWLINE = "\n"


def _stream_or_log(stream: TextIO | None, path: Path) -> TextIO:
    """*stream* when there is one; otherwise *path*, opened for appending."""
    if stream is not None:
        return stream
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("a", encoding=LOG_ENCODING, errors=LOG_ERRORS, newline=LOG_NEWLINE)


def open_missing_streams(
    out: TextIO | None, err: TextIO | None, state_dir: Path
) -> tuple[TextIO, TextIO]:
    """Each None stream replaced by an append-mode UTF-8 file in *state_dir*.

    The caller owns any file opened here; `missing_streams_opened` closes them for it.
    """
    return (
        _stream_or_log(out, state_dir / OUT_LOG_NAME),
        _stream_or_log(err, state_dir / ERR_LOG_NAME),
    )


@contextmanager
def missing_streams_opened(
    out: TextIO | None, err: TextIO | None, state_dir: Path
) -> Iterator[tuple[TextIO, TextIO]]:
    """`open_missing_streams`, closing on exit only the files it opened."""
    streams = open_missing_streams(out, err, state_dir)
    try:
        yield streams
    finally:
        for opened, given in zip(streams, (out, err), strict=True):
            if opened is not given:
                opened.close()
