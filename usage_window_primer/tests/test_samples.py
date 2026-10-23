"""The committed samples/ pair still matches what a tick produces."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _cli_fakes import PRIVATE_DIR_MODE as _PRIVATE_DIR_MODE  # noqa: E402 - import follows the sys.path bootstrap above
from _cli_fakes import make_seams  # noqa: E402 - same bootstrap
from usage_window_primer import EXIT_FAILED, EXIT_OK, main  # noqa: E402 - same bootstrap

_SAMPLES = Path(__file__).resolve().parents[1] / "samples"
# The estimate a tick records when it refuses a reported window end.
_WINDOW_LENGTH = timedelta(hours=5)


class SamplesTest(unittest.TestCase):
    """samples/README.md's command, run in-process."""

    def test_sample_tick_produces_the_expected_state(self) -> None:
        # The real clock, as the recipe runs: the recording's window end has long passed,
        # so the tick refuses it and records the estimate.
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            shutil.copytree(_SAMPLES / "state_dir", state_dir)
            state_dir.chmod(_PRIVATE_DIR_MODE)
            seams, runner, _out, _err = make_seams(Path(tmp), {}, now=datetime.now(UTC))
            argv = [
                "--mode",
                "tick",
                "--state-dir",
                str(state_dir),
                "--ping-output-file",
                str(_SAMPLES / "ping_output_ok.jsonl"),
            ]
            self.assertEqual(main(argv, seams=seams), EXIT_OK)
            actual = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
        expected = json.loads(
            (_SAMPLES / "expected_state.json").read_text(encoding="utf-8")
        )
        ran_at = datetime.fromisoformat(actual["last_attempt_at"])
        self.assertEqual(actual["last_ping_at"], actual["last_attempt_at"])
        self.assertEqual(
            datetime.fromisoformat(actual["window_ends_at"]), ran_at + _WINDOW_LENGTH
        )
        for moment in ("last_ping_at", "last_attempt_at", "window_ends_at"):
            del actual[moment], expected[moment]
        self.assertEqual(actual, expected)
        self.assertEqual(runner.commands(), [])

    def test_sample_error_tick_logs_the_expected_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            shutil.copytree(_SAMPLES / "state_dir", state_dir)
            state_dir.chmod(_PRIVATE_DIR_MODE)
            seams, runner, _out, _err = make_seams(Path(tmp), {})
            argv = [
                "--mode",
                "tick",
                "--state-dir",
                str(state_dir),
                "--ping-output-file",
                str(_SAMPLES / "ping_output_error.jsonl"),
            ]
            self.assertEqual(main(argv, seams=seams), EXIT_FAILED)
            log = (state_dir / "usage_window_primer.log").read_text(encoding="utf-8")
        (line,) = log.splitlines()
        # The leading timestamp is the moment the tick ran, so it is not compared.
        _moment, rest = line.split(" ", 1)
        expected = (_SAMPLES / "expected_log_error.txt").read_text(encoding="utf-8")
        self.assertEqual(rest, expected.strip())
        self.assertEqual(runner.commands(), [])


if __name__ == "__main__":
    unittest.main()
