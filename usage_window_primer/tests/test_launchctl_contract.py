"""The launchctl contract the agent read-back depends on, checked against the real launchctl.

This is the only live check of the launchctl boundary. Every other launchd test goes
through a ScriptedRunner, which proves how the exit codes are classified but not that
launchctl really produces them: `agent_loaded`'s tri-state read-back treats exit 113 as
"not loaded", a value measured once on one host, and a change to it would leave every
fake-driven suite green while install and uninstall misread launchd.

It calls launchctl and id by their absolute paths, as the tool does, which a stand-in
first on PATH cannot answer, so `USAGE_WINDOW_PRIMER_OFFLINE=1` skips it for a run that must not
reach the real ones, as in an export's proof. It is read-only: the one command is
a `launchctl print` of a label that is never installed, so nothing is loaded, unloaded
or written. It does not call `agent_loaded`, which is hardwired to the real label:
once the usage_window_primer is installed that label is loaded, so the answer would depend on this
machine's agent list instead of the contract.
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from collections.abc import Mapping
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TOOL_ROOT / "src"))

from launch_agent import (  # noqa: E402 - import follows the sys.path bootstrap above
    LAUNCHCTL,
    LAUNCHCTL_NOT_FOUND_EXIT,
    LAUNCHCTL_TIMEOUT_SECONDS,
    LAUNCHD_LABEL,
    user_domain,
)
from process_run import run_process  # noqa: E402 - same bootstrap

# A label no install ever creates, so the print below can only ever be "not found".
_NEVER_INSTALLED_LABEL = f"{LAUNCHD_LABEL}.never-installed"
#: Set to 1 to skip the live test on a run that must not reach the real launchctl and
#: id: an export's proof sets it, because the absolute paths called below escape the
#: logging stand-in it puts first on PATH for each.
_OFFLINE_ENV = "USAGE_WINDOW_PRIMER_OFFLINE"


def _contract_skip_reason(
    environ: Mapping[str, str], platform: str, launchctl: str | None
) -> str | None:
    """Why the live contract test must not run, else None.

    *launchctl* is `LAUNCHCTL` when that absolute path names an executable, which is
    how the tool runs it, or None when it does not.
    """
    if environ.get(_OFFLINE_ENV) == "1":
        return f"{_OFFLINE_ENV}=1 - live launchctl contract test skipped"
    if platform != "darwin" or launchctl is None:
        return f"needs macOS with an executable {LAUNCHCTL}"
    return None


_CONTRACT_SKIP = _contract_skip_reason(
    os.environ, sys.platform, shutil.which(LAUNCHCTL)
)


class ContractSkipTest(unittest.TestCase):
    """When the live contract test below runs, and why it is skipped otherwise."""

    def test_the_offline_switch_skips_it_even_on_macos(self) -> None:
        self.assertEqual(
            _contract_skip_reason(
                {"USAGE_WINDOW_PRIMER_OFFLINE": "1"}, "darwin", "/bin/launchctl"
            ),
            "USAGE_WINDOW_PRIMER_OFFLINE=1 - live launchctl contract test skipped",
        )

    def test_another_platform_skips_it(self) -> None:
        self.assertEqual(
            _contract_skip_reason({}, "linux", "/bin/launchctl"),
            # Regression: it said "launchctl on PATH" after the lookup moved to the
            # absolute path, which no PATH entry changes.
            "needs macOS with an executable /bin/launchctl",
        )

    def test_a_missing_launchctl_skips_it(self) -> None:
        self.assertEqual(
            _contract_skip_reason({}, "darwin", None),
            "needs macOS with an executable /bin/launchctl",
        )

    def test_it_runs_on_macos_with_launchctl_and_the_switch_unset(self) -> None:
        self.assertIsNone(
            _contract_skip_reason(
                {"USAGE_WINDOW_PRIMER_OFFLINE": "0"}, "darwin", "/bin/launchctl"
            )
        )


@unittest.skipIf(_CONTRACT_SKIP is not None, _CONTRACT_SKIP or "")
class LaunchctlContractTest(unittest.TestCase):
    """launchctl reports a service that does not exist with the exit code we classify."""

    def test_print_of_a_missing_service_exits_not_found(self) -> None:
        domain = user_domain(run_process)
        code, _out, _err = run_process(
            [LAUNCHCTL, "print", f"{domain}/{_NEVER_INSTALLED_LABEL}"],
            LAUNCHCTL_TIMEOUT_SECONDS,
            None,
            None,
        )
        self.assertEqual(code, LAUNCHCTL_NOT_FOUND_EXIT)


if __name__ == "__main__":
    unittest.main()
