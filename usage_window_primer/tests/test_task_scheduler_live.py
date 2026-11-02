r"""One real Task Scheduler round trip through the real `schtasks`: the boundary proof.

Every other Windows suite drives `TaskSchedulerBackend` through a scripted runner, which
proves the argv and the parsing but not the CONTRACT: Task Scheduler refusing the
generated XML, or wording a query differently, would leave them all green. This test
installs a probe task, reads it back as registered, uninstalls it and reads it back as
gone.

Opt-in with ``USAGE_WINDOW_PRIMER_LIVE_TASK=1`` because it writes to the real Task Scheduler; it is
skipped, not failed, without that variable or off Windows. The probe never touches the
real ``\UsageWindowPrimer\Tick`` task: it lives under its own folder with a random name, its
state directory is a temporary one, and a cleanup deletes it even when an assertion
fails.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TOOL_ROOT / "src"))

from process_run import run_process  # noqa: E402 - import follows the sys.path bootstrap above
from scheduler import InstallRequest  # noqa: E402 - same bootstrap
from task_agent import (  # noqa: E402 - same bootstrap
    schtasks_path,
    SCHTASKS_TIMEOUT_SECONDS,
    TaskSchedulerBackend,
)

LIVE = (
    sys.platform == "win32" and os.environ.get("USAGE_WINDOW_PRIMER_LIVE_TASK") == "1"
)
PROBE_FOLDER = "\\UsageWindowPrimerProbe\\"
# Eight hex digits keep concurrent or leftover probes apart without a long name.
PROBE_SUFFIX_LENGTH = 8
POLL_MINUTES = 30
# The repeating trigger starts a day ahead, so Task Scheduler has no slot to run (and,
# through StartWhenAvailable, no missed slot to catch up) while the probe is registered:
# the test proves registration and never sends a real ping.
START_DELAY = timedelta(days=1)


def _delete_probe(task_name: str) -> None:
    """Delete *task_name* directly, ignoring the result: the cleanup of last resort."""
    run_process(
        [schtasks_path(os.environ), "/Delete", "/TN", task_name, "/F"],
        SCHTASKS_TIMEOUT_SECONDS,
        None,
        None,
    )


@unittest.skipUnless(LIVE, "set USAGE_WINDOW_PRIMER_LIVE_TASK=1 on Windows")
class LiveTaskSchedulerTest(unittest.TestCase):
    """Install, read back, uninstall and read back a probe task in the real scheduler."""

    def test_a_probe_task_registers_and_unregisters(self) -> None:
        task_name = f"{PROBE_FOLDER}Probe{uuid.uuid4().hex[:PROBE_SUFFIX_LENGTH]}"
        self.addCleanup(_delete_probe, task_name)
        backend = TaskSchedulerBackend(
            run_process,
            os.environ,
            task_name=task_name,
            now_local=lambda: datetime.now() + START_DELAY,
        )
        with tempfile.TemporaryDirectory() as tmp:
            request = InstallRequest(
                python=sys.executable,
                script=str(_TOOL_ROOT / "usage_window_primer.py"),
                state_dir=Path(tmp),
                poll_minutes=POLL_MINUTES,
            )
            backend.install(request)
            self.assertTrue(backend.describe_state().startswith("registered"))
            backend.uninstall()
            self.assertEqual(backend.describe_state(), "not registered")


if __name__ == "__main__":
    unittest.main()
