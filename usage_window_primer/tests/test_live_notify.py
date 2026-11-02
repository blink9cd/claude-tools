"""One real notification through the real `osascript`: the boundary no fake can prove.

The unit suites prove what the alert says and that text reaches `osascript` unaltered,
but not that macOS SHOWS it: a script `osascript` refuses, or a notification permission
it lacks, would leave them all green. Opt-in with ``USAGE_WINDOW_PRIMER_LIVE_NOTIFY=1`` because it
puts a notification on screen, which someone must be watching to confirm; it is skipped,
not failed, without that variable or without `osascript` on PATH.
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TOOL_ROOT / "src"))

from failure_alert import OSASCRIPT, Alert, send_alert  # noqa: E402 - import follows the sys.path bootstrap above
from process_run import run_process  # noqa: E402 - same bootstrap

LIVE = os.environ.get("USAGE_WINDOW_PRIMER_LIVE_NOTIFY") == "1"
HAS_OSASCRIPT = shutil.which(OSASCRIPT) is not None


@unittest.skipUnless(
    LIVE and HAS_OSASCRIPT,
    "set USAGE_WINDOW_PRIMER_LIVE_NOTIFY=1 with osascript on PATH",
)
class LiveNotifyTest(unittest.TestCase):
    """A real notification is accepted; the person watching confirms it appeared."""

    def test_a_real_notification_is_accepted(self) -> None:
        alert = Alert("Claude usage window primer (test)", "Live notification test")
        self.assertIsNone(send_alert(run_process, alert))


if __name__ == "__main__":
    unittest.main()
