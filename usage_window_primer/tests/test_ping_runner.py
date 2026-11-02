"""The lean ping: its exact command, its environment, and how its exit is classified."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TOOL_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import (  # noqa: E402 - import follows the sys.path bootstrap above
    CLI_ERROR_RESULT,
    ERROR_RESULT,
    RESET_AT,
    RESET_EPOCH,
    SUCCESS_RESULT,
    SYSTEM_INIT,
    ScriptedRunner,
    rate_limit_line,
    stream,
)
from ping_output import OUTCOME_FAILED, OUTCOME_LIMITED, OUTCOME_OK  # noqa: E402 - same bootstrap
from ping_runner import (  # noqa: E402 - same bootstrap
    DISABLE_CLAUDE_MDS_ENV,
    PING_ENV_DROPPED,
    PING_TIMEOUT_SECONDS,
    build_ping_command,
    build_ping_env,
    run_ping,
)
from state_store import PrimerConfig  # noqa: E402 - same bootstrap

_CLAUDE = "/opt/fake/bin/claude"
_CONFIG = PrimerConfig(None, "haiku", "Reply with the single word: ok", 5, _CLAUDE)
_STATE_DIR = Path("/opt/fake/state")
_OK_STREAM = stream(rate_limit_line("allowed", RESET_EPOCH), SUCCESS_RESULT)


def _runner(result: tuple[int, str, str]) -> ScriptedRunner:
    return ScriptedRunner({(_CLAUDE, "-p"): [result]})


class PingCommandTest(unittest.TestCase):
    """The command and environment, restated literally from usage_window_primer.md's "The ping"."""

    def test_command_is_the_measured_lean_ping(self) -> None:
        self.assertEqual(
            build_ping_command(_CLAUDE, "haiku", "Reply with the single word: ok"),
            [
                _CLAUDE,
                "-p",
                "Reply with the single word: ok",
                "--model",
                "haiku",
                "--setting-sources",
                "",
                "--tools",
                "",
                # No --mcp-config beside it, so no MCP server is started at all.
                "--strict-mcp-config",
                "--no-session-persistence",
                "--max-turns",
                "1",
                "--output-format",
                "stream-json",
                "--verbose",
            ],
        )

    def test_env_adds_the_instruction_file_switch_without_mutating_the_base(
        self,
    ) -> None:
        base = {"PATH": "/usr/bin", "HOME": "/opt/fake/home"}
        env = build_ping_env(base)
        self.assertEqual(env, {**base, DISABLE_CLAUDE_MDS_ENV: "1"})
        self.assertNotIn(DISABLE_CLAUDE_MDS_ENV, base)

    def test_env_drops_every_variable_that_reroutes_or_rebills_the_ping(self) -> None:
        # Regression: the whole inherited environment used to pass through, so an API
        # key or another endpoint in the caller's shell made the ping bill API credit,
        # or go elsewhere, while the log still read `ok`.
        rerouting = {
            "ANTHROPIC_API_KEY": "<placeholder>",
            "ANTHROPIC_AUTH_TOKEN": "<placeholder>",
            "ANTHROPIC_BASE_URL": "https://example.invalid",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_USE_VERTEX": "1",
        }
        base = {"PATH": "/usr/bin", **rerouting}
        env = build_ping_env(base)
        self.assertEqual(env, {"PATH": "/usr/bin", DISABLE_CLAUDE_MDS_ENV: "1"})
        self.assertEqual(PING_ENV_DROPPED, frozenset(rerouting))
        self.assertIn("ANTHROPIC_API_KEY", base)


class RunPingTest(unittest.TestCase):
    """One ping through an injected runner."""

    def test_run_ping_runs_from_the_state_dir_with_the_timeout(self) -> None:
        runner = _runner((0, _OK_STREAM, ""))
        run_ping(_CONFIG, _STATE_DIR, runner=runner, base_env={"PATH": "/usr/bin"})
        (call,) = runner.calls
        self.assertEqual(
            call.argv, build_ping_command(_CLAUDE, "haiku", _CONFIG.prompt)
        )
        self.assertEqual((call.timeout, call.cwd), (PING_TIMEOUT_SECONDS, _STATE_DIR))
        self.assertEqual(call.env, {"PATH": "/usr/bin", DISABLE_CLAUDE_MDS_ENV: "1"})

    def test_a_clean_exit_with_a_success_stream_is_ok(self) -> None:
        outcome = run_ping(
            _CONFIG, _STATE_DIR, runner=_runner((0, _OK_STREAM, "")), base_env={}
        )
        self.assertEqual((outcome.kind, outcome.resets_at), (OUTCOME_OK, RESET_AT))

    def test_a_limited_stream_is_trusted_from_a_failing_exit(self) -> None:
        text = stream(rate_limit_line("rejected", RESET_EPOCH), ERROR_RESULT)
        outcome = run_ping(
            _CONFIG, _STATE_DIR, runner=_runner((1, text, "limit")), base_env={}
        )
        self.assertEqual((outcome.kind, outcome.resets_at), (OUTCOME_LIMITED, RESET_AT))

    def test_a_failing_exit_is_failed_even_with_a_success_stream(self) -> None:
        # Only a FAILED result's reason leads stderr: a success result's "ping succeeded"
        # must not open a failure's log line, so stderr stands alone.
        runner = _runner((1, _OK_STREAM, "boom\n"))
        outcome = run_ping(_CONFIG, _STATE_DIR, runner=runner, base_env={})
        self.assertEqual(
            (outcome.kind, outcome.detail), (OUTCOME_FAILED, "exit 1: boom")
        )

    def test_a_failing_exit_with_empty_stderr_logs_the_stream_reason(self) -> None:
        # The CLI can report its error as a stream-json result on stdout alone; the
        # log line must still say why, not end at "exit 1: ".
        runner = _runner((1, stream(ERROR_RESULT), ""))
        outcome = run_ping(_CONFIG, _STATE_DIR, runner=runner, base_env={})
        self.assertEqual(outcome.kind, OUTCOME_FAILED)
        self.assertEqual(
            outcome.detail, "exit 1: result subtype 'error_during_execution'"
        )

    def test_a_failing_exit_with_a_cli_error_result_logs_the_cli_message(self) -> None:
        # The measured expired-login shape: exit 1, empty stderr, and a result event whose
        # subtype still reads "success". The log line must carry the CLI's own words.
        runner = _runner((1, stream(SYSTEM_INIT, CLI_ERROR_RESULT), ""))
        outcome = run_ping(_CONFIG, _STATE_DIR, runner=runner, base_env={})
        self.assertEqual(outcome.kind, OUTCOME_FAILED)
        self.assertEqual(outcome.detail, "exit 1: CLI error: OAuth session expired")

    def test_a_clean_exit_with_no_result_is_failed(self) -> None:
        outcome = run_ping(
            _CONFIG, _STATE_DIR, runner=_runner((0, "garbage", "")), base_env={}
        )
        self.assertEqual(outcome.kind, OUTCOME_FAILED)

    def test_failure_detail_is_capped(self) -> None:
        runner = _runner((127, "", "x" * 1000))
        outcome = run_ping(_CONFIG, _STATE_DIR, runner=runner, base_env={})
        self.assertEqual(outcome.detail, "exit 127: " + "x" * 200)


def _detail(result: tuple[int, str, str]) -> str:
    """The detail of one ping whose runner answers *result*."""
    return run_ping(_CONFIG, _STATE_DIR, runner=_runner(result), base_env={}).detail


class FailingExitDetailTest(unittest.TestCase):
    """Which evidence a failing exit's log detail carries, and in what order."""

    def test_stderr_never_hides_the_cli_error(self) -> None:
        # The CLI's own error leads, so the log cap cuts stderr before it cuts the error.
        detail = _detail((1, stream(SYSTEM_INIT, CLI_ERROR_RESULT), "warn\n"))
        self.assertEqual(
            detail, "exit 1: CLI error: OAuth session expired; stderr: warn"
        )

    def test_stderr_beside_a_result_is_capped_after_the_error(self) -> None:
        detail = _detail((1, stream(CLI_ERROR_RESULT), "w" * 1000))
        self.assertEqual(
            detail, "exit 1: CLI error: OAuth session expired; stderr: " + "w" * 200
        )

    def test_stderr_alone_when_the_stream_held_no_result(self) -> None:
        self.assertEqual(_detail((1, stream(SYSTEM_INIT), "boom\n")), "exit 1: boom")

    def test_no_result_and_no_stderr_says_so(self) -> None:
        self.assertEqual(
            _detail((1, stream(SYSTEM_INIT), "")),
            "exit 1: no result event in the ping output",
        )


class TimedOutPingTest(unittest.TestCase):
    """A timed-out ping's detail names the last event it wrote before the kill."""

    def test_a_timeout_names_the_last_event(self) -> None:
        detail = _detail((124, stream(SYSTEM_INIT), "timed out after 120s"))
        self.assertEqual(
            detail, "exit 124: timed out after 120s; last event: system/init"
        )

    def test_a_timeout_with_no_output_says_so(self) -> None:
        detail = _detail((124, "", "timed out after 120s"))
        self.assertEqual(
            detail, "exit 124: timed out after 120s; last event: no output"
        )

    def test_a_timeout_caps_stderr_but_keeps_the_last_event(self) -> None:
        message = "timed out after 120s; stderr: " + "w" * 1000
        detail = _detail((124, stream(SYSTEM_INIT), message))
        self.assertEqual(detail, f"exit 124: {message[:200]}; last event: system/init")

    def test_a_timed_out_limited_stream_is_still_limited(self) -> None:
        text = stream(rate_limit_line("rejected", RESET_EPOCH))
        runner = _runner((124, text, "timed out after 120s"))
        outcome = run_ping(_CONFIG, _STATE_DIR, runner=runner, base_env={})
        self.assertEqual((outcome.kind, outcome.resets_at), (OUTCOME_LIMITED, RESET_AT))


if __name__ == "__main__":
    unittest.main()
