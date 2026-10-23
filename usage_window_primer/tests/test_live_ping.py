"""One real ping through the real `claude` CLI -- the boundary no fake can prove.

Every other suite fakes the runner, which proves the classification but not the
CONTRACT: a renamed event, a moved field or a changed flag would leave them all green.
Opt-in with ``USAGE_WINDOW_PRIMER_LIVE_PING=1`` because it spends real subscription usage; it is
skipped, not failed, without that variable or without `claude` on PATH.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TOOL_ROOT / "src"))

from ping_output import OUTCOME_FAILED  # noqa: E402 - import follows the sys.path bootstrap above
from ping_runner import run_ping  # noqa: E402 - same bootstrap
from state_store import PrimerConfig  # noqa: E402 - same bootstrap

LIVE = os.environ.get("USAGE_WINDOW_PRIMER_LIVE_PING") == "1"
CLAUDE = shutil.which("claude")


@unittest.skipUnless(
    LIVE and CLAUDE, "set USAGE_WINDOW_PRIMER_LIVE_PING=1 with claude on PATH"
)
class LivePingTest(unittest.TestCase):
    """A real ping reports a future five-hour window end."""

    def test_a_real_ping_reports_a_future_window_end(self) -> None:
        assert CLAUDE is not None
        config = PrimerConfig(
            None, "haiku", "Reply with the single word: ok", 5, CLAUDE
        )
        with tempfile.TemporaryDirectory() as tmp:
            outcome = run_ping(config, Path(tmp))
        self.assertNotEqual(outcome.kind, OUTCOME_FAILED, outcome.detail)
        self.assertIsNotNone(outcome.resets_at)
        assert outcome.resets_at is not None
        self.assertGreater(outcome.resets_at, datetime.now(UTC))


if __name__ == "__main__":
    unittest.main()
