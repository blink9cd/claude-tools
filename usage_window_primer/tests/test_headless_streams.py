"""Output with no console: each missing stream becomes a log file in the state directory.

Under ``pythonw.exe`` the process has no stdout or stderr, so without these files every
``ERROR:`` and ``WARNING:`` line a scheduled tick writes would be lost.
"""

from __future__ import annotations

import dataclasses
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _cli_fakes import make_seams  # noqa: E402 - import follows the sys.path bootstrap above
from headless_streams import (  # noqa: E402 - same bootstrap
    ERR_LOG_NAME,
    OUT_LOG_NAME,
    missing_streams_opened,
    open_missing_streams,
)
from usage_window_primer import EXIT_USAGE, main  # noqa: E402 - same bootstrap


class OpenMissingStreamsTest(unittest.TestCase):
    """A None stream is replaced by an append-mode file; a given one is kept."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name) / "state"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_both_missing_open_appending_files(self) -> None:
        self.state_dir.mkdir()
        (self.state_dir / ERR_LOG_NAME).write_text(
            "earlier\n", encoding="utf-8", newline="\n"
        )
        out, err = open_missing_streams(None, None, self.state_dir)
        with out, err:
            print("to out", file=out)
            print("to err", file=err)
        self.assertEqual(
            (self.state_dir / OUT_LOG_NAME).read_text(encoding="utf-8"), "to out\n"
        )
        self.assertEqual(
            (self.state_dir / ERR_LOG_NAME).read_text(encoding="utf-8"),
            "earlier\nto err\n",
        )

    def test_given_streams_are_returned_unchanged(self) -> None:
        given_out, given_err = io.StringIO(), io.StringIO()
        out, err = open_missing_streams(given_out, given_err, self.state_dir)
        self.assertIs(out, given_out)
        self.assertIs(err, given_err)
        self.assertFalse(self.state_dir.exists())

    def test_only_the_missing_stream_gets_a_file(self) -> None:
        given_out = io.StringIO()
        out, err = open_missing_streams(given_out, None, self.state_dir)
        with err:
            self.assertIs(out, given_out)
        self.assertTrue((self.state_dir / ERR_LOG_NAME).exists())
        self.assertFalse((self.state_dir / OUT_LOG_NAME).exists())

    def test_context_closes_only_what_it_opened(self) -> None:
        given_out = io.StringIO()
        with missing_streams_opened(given_out, None, self.state_dir) as (out, err):
            self.assertIs(out, given_out)
        self.assertTrue(err.closed)
        self.assertFalse(given_out.closed)


class HeadlessMainTest(unittest.TestCase):
    """main with no console writes its ERROR line to task.err.log."""

    def test_tick_without_install_logs_the_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            state_dir = home / "state"
            seams, _runner, _out, _err = make_seams(home, {})
            seams = dataclasses.replace(seams, out=None, err=None)
            code = main(["--mode", "tick", "--state-dir", str(state_dir)], seams=seams)
            logged = (state_dir / ERR_LOG_NAME).read_text(encoding="utf-8")
        self.assertEqual(code, EXIT_USAGE)
        self.assertTrue(logged.startswith("ERROR:"), logged)


if __name__ == "__main__":
    unittest.main()
