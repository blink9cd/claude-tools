"""The launchd agent: its plist, and loading / unloading confirmed by read-back.

Every launchctl and id call goes to a ScriptedRunner, so no test touches the real
agent list.
"""

from __future__ import annotations

import plistlib
import sys
import tempfile
import unittest
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TOOL_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import RunResult, ScriptedRunner  # noqa: E402 - import follows the sys.path bootstrap above
from launch_agent import (  # noqa: E402 - same bootstrap
    CONFIRM_ATTEMPTS,
    CONFIRM_INTERVAL_SECONDS,
    ID_COMMAND,
    LAUNCHCTL,
    LAUNCHD_LABEL,
    AgentError,
    agent_loaded,
    build_plist,
    calendar_minutes,
    install_agent,
    plist_path,
    uninstall_agent,
    user_domain,
)

_OK: RunResult = (0, "", "")
_NOT_FOUND: RunResult = (113, "", "Could not find service")
# process_run's report of a launchctl that did not answer in time: a failed read, which
# is neither "loaded" nor "not loaded".
_TIMED_OUT: RunResult = (124, "", "timed out after 30s")
_TARGET = f"gui/501/{LAUNCHD_LABEL}"


def _answers(**overrides: list[RunResult]) -> dict[tuple[str, str], list[RunResult]]:
    answers: dict[tuple[str, str], list[RunResult]] = {
        (ID_COMMAND, "-u"): [(0, "501\n", "")],
        (LAUNCHCTL, "bootout"): [_OK],
        (LAUNCHCTL, "bootstrap"): [_OK],
        (LAUNCHCTL, "print"): [_OK],
    }
    for key, queue in overrides.items():
        answers[(LAUNCHCTL, key)] = queue
    return answers


class PlistTest(unittest.TestCase):
    """The plist, built for several poll intervals."""

    def test_plist_path_is_the_user_launch_agents_dir(self) -> None:
        home = Path("/opt/fake/home")
        expected = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        self.assertEqual(plist_path(home), expected)

    def test_calendar_minutes_for_divisors_of_sixty(self) -> None:
        rows = {
            5: list(range(0, 60, 5)),
            15: [0, 15, 30, 45],
            60: [0],
            1: list(range(60)),
        }
        for poll, expected in rows.items():
            with self.subTest(poll_minutes=poll):
                self.assertEqual(calendar_minutes(poll), expected)

    def test_calendar_minutes_rejects_non_divisors(self) -> None:
        for poll in (0, -5, 7, 61):
            with self.subTest(poll_minutes=poll), self.assertRaises(ValueError):
                calendar_minutes(poll)

    def test_plist_runs_a_tick_on_every_slot_and_at_load(self) -> None:
        state_dir = Path("/opt/fake/state")
        job = plistlib.loads(
            build_plist(
                "/opt/fake/python3", "/opt/fake/usage_window_primer.py", state_dir, 15
            )
        )
        self.assertEqual(
            job,
            {
                "Label": LAUNCHD_LABEL,
                "ProgramArguments": [
                    "/opt/fake/python3",
                    "/opt/fake/usage_window_primer.py",
                    "--mode",
                    "tick",
                    "--state-dir",
                    "/opt/fake/state",
                ],
                "StartCalendarInterval": [
                    {"Minute": 0},
                    {"Minute": 15},
                    {"Minute": 30},
                    {"Minute": 45},
                ],
                "RunAtLoad": True,
                "StandardOutPath": "/opt/fake/state/launchd.out.log",
                "StandardErrorPath": "/opt/fake/state/launchd.err.log",
            },
        )

    def test_plist_never_uses_start_interval(self) -> None:
        # StartInterval misses firings while asleep (man launchd.plist); the catch-up
        # ping on wake depends on calendar firings, which coalesce instead.
        job = plistlib.loads(build_plist("p", "s", Path("/opt/fake/state"), 5))
        self.assertNotIn("StartInterval", job)


class UserDomainTest(unittest.TestCase):
    """The GUI domain comes from `id -u`."""

    def test_user_domain_reads_id(self) -> None:
        self.assertEqual(user_domain(ScriptedRunner(_answers())), "gui/501")

    def test_user_domain_rejects_a_failed_or_odd_id(self) -> None:
        # A timeout keeps the child's partial stdout, so a timed-out id can carry a
        # plausible uid: only the exit code may decide that none was read.
        timed_out_with_output: RunResult = (124, "501\n", "timed out after 30s")
        for result in (
            (1, "", "id: failed"),
            (0, "not-a-number\n", ""),
            timed_out_with_output,
        ):
            with self.subTest(result=result):
                runner = ScriptedRunner({(ID_COMMAND, "-u"): [result]})
                with self.assertRaises(AgentError):
                    user_domain(runner)


class HostBinaryTest(unittest.TestCase):
    """launchctl and id are run by absolute path, never looked up on PATH."""

    def test_the_binaries_are_the_system_ones(self) -> None:
        # Regression: both ran by bare name, so a PATH entry ahead of the system folders
        # chose which launchctl loaded the agent and which id named its domain.
        self.assertEqual((LAUNCHCTL, ID_COMMAND), ("/bin/launchctl", "/usr/bin/id"))

    def test_every_command_an_install_runs_names_one_of_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = ScriptedRunner(_answers(print=[_NOT_FOUND, _OK]))
            plist = Path(tmp) / "a.plist"
            install_agent(plist, b"<plist/>", runner, sleep=lambda _seconds: None)
        self.assertEqual(
            {argv[0] for argv in runner.commands()}, {LAUNCHCTL, ID_COMMAND}
        )


class AgentLoadedTest(unittest.TestCase):
    """`launchctl print` answers loaded (0) or not loaded (113); anything else is no answer."""

    def test_agent_loaded_maps_found_and_not_found(self) -> None:
        for result, expected in ((_OK, True), (_NOT_FOUND, False)):
            with self.subTest(result=result):
                runner = ScriptedRunner(_answers(print=[result]))
                self.assertEqual(agent_loaded(runner, "gui/501"), expected)
                self.assertEqual(runner.commands(), [[LAUNCHCTL, "print", _TARGET]])

    def test_agent_loaded_raises_on_any_other_exit(self) -> None:
        runner = ScriptedRunner(_answers(print=[_TIMED_OUT]))
        with self.assertRaises(AgentError) as caught:
            agent_loaded(runner, "gui/501")
        self.assertIn("124", str(caught.exception))
        self.assertIn("timed out", str(caught.exception))


class InstallUninstallTest(unittest.TestCase):
    """Act, then confirm by polling `launchctl print`."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.plist_file = (
            Path(self._tmp.name) / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        )
        self.sleeps: list[float] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_install_writes_the_plist_then_reloads_and_confirms(self) -> None:
        # A first install: bootout fails (nothing loaded), the read-back confirms nothing
        # is loaded, and only then does bootstrap run and the second read-back find it.
        runner = ScriptedRunner(
            _answers(bootout=[(5, "", "not loaded")], print=[_NOT_FOUND, _OK])
        )
        install_agent(self.plist_file, b"<plist/>", runner, sleep=self.sleeps.append)
        self.assertEqual(self.plist_file.read_bytes(), b"<plist/>")
        self.assertEqual(
            runner.commands(),
            [
                [ID_COMMAND, "-u"],
                [LAUNCHCTL, "bootout", _TARGET],
                [LAUNCHCTL, "print", _TARGET],
                [LAUNCHCTL, "bootstrap", "gui/501", str(self.plist_file)],
                [LAUNCHCTL, "print", _TARGET],
            ],
        )
        self.assertEqual(self.sleeps, [])

    def test_install_waits_for_the_old_instance_to_unload_before_bootstrap(
        self,
    ) -> None:
        # A reinstall whose old instance is still tearing down on the first read.
        runner = ScriptedRunner(_answers(print=[_OK, _NOT_FOUND, _OK]))
        install_agent(self.plist_file, b"<plist/>", runner, sleep=self.sleeps.append)
        self.assertEqual(
            runner.commands(),
            [
                [ID_COMMAND, "-u"],
                [LAUNCHCTL, "bootout", _TARGET],
                [LAUNCHCTL, "print", _TARGET],
                [LAUNCHCTL, "print", _TARGET],
                [LAUNCHCTL, "bootstrap", "gui/501", str(self.plist_file)],
                [LAUNCHCTL, "print", _TARGET],
            ],
        )
        self.assertEqual(self.sleeps, [CONFIRM_INTERVAL_SECONDS])

    def test_install_raises_when_bootout_never_unloads(self) -> None:
        runner = ScriptedRunner(
            _answers(bootout=[(36, "", "Operation now in progress")], print=[_OK])
        )
        with self.assertRaises(AgentError) as caught:
            install_agent(
                self.plist_file, b"<plist/>", runner, sleep=self.sleeps.append
            )
        self.assertIn("Operation now in progress", str(caught.exception))
        self.assertNotIn("bootstrap", [argv[1] for argv in runner.commands()])

    def test_install_raises_when_bootstrap_fails(self) -> None:
        runner = ScriptedRunner(
            _answers(bootstrap=[(5, "", "Bootstrap failed: 5")], print=[_NOT_FOUND])
        )
        with self.assertRaises(AgentError) as caught:
            install_agent(
                self.plist_file, b"<plist/>", runner, sleep=self.sleeps.append
            )
        self.assertIn("Bootstrap failed", str(caught.exception))
        # No read-back after a failed bootstrap: the pre-bootstrap read is the last one.
        self.assertEqual(
            runner.commands()[-2:],
            [
                [LAUNCHCTL, "print", _TARGET],
                [LAUNCHCTL, "bootstrap", "gui/501", str(self.plist_file)],
            ],
        )

    def test_install_polls_until_the_agent_is_loaded(self) -> None:
        # The first _NOT_FOUND is the pre-bootstrap read; two more follow bootstrap.
        runner = ScriptedRunner(
            _answers(print=[_NOT_FOUND, _NOT_FOUND, _NOT_FOUND, _OK])
        )
        install_agent(self.plist_file, b"<plist/>", runner, sleep=self.sleeps.append)
        self.assertEqual(self.sleeps, [CONFIRM_INTERVAL_SECONDS] * 2)

    def test_install_raises_when_the_agent_never_loads(self) -> None:
        runner = ScriptedRunner(_answers(print=[_NOT_FOUND]))
        with self.assertRaises(AgentError) as caught:
            install_agent(
                self.plist_file, b"<plist/>", runner, sleep=self.sleeps.append
            )
        self.assertIn("loaded", str(caught.exception))
        # The pre-bootstrap read settles at once; every sleep belongs to the final wait.
        self.assertEqual(len(self.sleeps), CONFIRM_ATTEMPTS)

    def test_uninstall_raises_and_keeps_the_plist_when_the_read_back_fails(
        self,
    ) -> None:
        self.plist_file.parent.mkdir(parents=True)
        self.plist_file.write_bytes(b"<plist/>")
        runner = ScriptedRunner(_answers(print=[_TIMED_OUT]))
        with self.assertRaises(AgentError) as caught:
            uninstall_agent(self.plist_file, runner, sleep=self.sleeps.append)
        self.assertTrue(self.plist_file.exists())
        self.assertIn("timed out", str(caught.exception))
        # A failed read is retried as "not settled yet", never taken as an answer.
        self.assertEqual(len(self.sleeps), CONFIRM_ATTEMPTS)

    def test_uninstall_retries_an_unreadable_read_back(self) -> None:
        self.plist_file.parent.mkdir(parents=True)
        self.plist_file.write_bytes(b"<plist/>")
        runner = ScriptedRunner(_answers(print=[_TIMED_OUT, _NOT_FOUND]))
        uninstall_agent(self.plist_file, runner, sleep=self.sleeps.append)
        self.assertFalse(self.plist_file.exists())
        self.assertEqual(self.sleeps, [CONFIRM_INTERVAL_SECONDS])

    def test_uninstall_unloads_confirms_and_removes_the_plist(self) -> None:
        self.plist_file.parent.mkdir(parents=True)
        self.plist_file.write_bytes(b"<plist/>")
        runner = ScriptedRunner(_answers(print=[_NOT_FOUND]))
        uninstall_agent(self.plist_file, runner, sleep=self.sleeps.append)
        self.assertFalse(self.plist_file.exists())
        self.assertEqual(runner.commands()[1], [LAUNCHCTL, "bootout", _TARGET])

    def test_uninstall_raises_and_keeps_the_plist_while_still_loaded(self) -> None:
        self.plist_file.parent.mkdir(parents=True)
        self.plist_file.write_bytes(b"<plist/>")
        runner = ScriptedRunner(_answers(print=[_OK]))
        with self.assertRaises(AgentError):
            uninstall_agent(self.plist_file, runner, sleep=self.sleeps.append)
        self.assertTrue(self.plist_file.exists())

    def test_uninstall_tolerates_an_already_missing_plist(self) -> None:
        runner = ScriptedRunner(_answers(print=[_NOT_FOUND]))
        uninstall_agent(self.plist_file, runner, sleep=self.sleeps.append)
        self.assertFalse(self.plist_file.exists())


if __name__ == "__main__":
    unittest.main()
