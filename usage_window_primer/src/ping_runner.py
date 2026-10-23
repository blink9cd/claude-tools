"""Build and run the lean headless `claude -p` ping, and classify what came back.

Every flag was measured: together they cut a ping from 31,613
input tokens to 6,710 while still authenticating with the subscription (2026-09-13; the
same ping measured 3,697 on 2026-10-19). `--bare` is
deliberately absent -- it refuses subscription auth, so a bare ping would never open a
subscription window at all.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from ping_output import (
    NO_RESULT_DETAIL,
    OUTCOME_FAILED,
    OUTCOME_LIMITED,
    PingOutcome,
    describe_last_event,
    parse_ping_output,
)
from process_run import TIMEOUT_EXIT_CODE, Runner, run_process
from state_store import PrimerConfig

PING_TIMEOUT_SECONDS = 120.0
# Keeps every CLAUDE.md -- user, project and local -- out of the ping's context.
DISABLE_CLAUDE_MDS_ENV = "CLAUDE_CODE_DISABLE_CLAUDE_MDS"
# Removed from the inherited environment: each makes the CLI authenticate with an API key
# or a token instead of the subscription, or send the call to another endpoint or cloud
# provider. The ping would then bill API credit, or open no subscription window at all,
# while its result still read as a success and the log as `ok`.
PING_ENV_DROPPED = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
    }
)
# The longest slice of a failing ping's stderr carried into its log line.
_DETAIL_MAX_CHARS = 200


def build_ping_command(claude_path: str, model: str, prompt: str) -> list[str]:
    """The argument vector for one lean ping."""
    return [
        claude_path,
        "-p",
        prompt,
        "--model",
        model,
        "--setting-sources",
        "",
        "--tools",
        "",
        # With no --mcp-config beside it, no MCP server starts: the user-scope servers
        # would otherwise launch, each a process of its own, on every ping.
        "--strict-mcp-config",
        "--no-session-persistence",
        "--max-turns",
        "1",
        "--output-format",
        "stream-json",
        "--verbose",
    ]


def build_ping_env(base_env: Mapping[str, str]) -> dict[str, str]:
    """*base_env* without `PING_ENV_DROPPED`, plus the switch keeping instruction files out."""
    kept = {
        name: value for name, value in base_env.items() if name not in PING_ENV_DROPPED
    }
    return {**kept, DISABLE_CLAUDE_MDS_ENV: "1"}


def _failure_detail(code: int, stdout: str, stderr: str, outcome: PingOutcome) -> str:
    """The log detail for a failing exit whose stream was not `limited`.

    A timeout ends with the last event the ping wrote, which shows how far the CLI got
    before the bound. Otherwise, when the stream held a failed result event, its reason
    leads and stderr follows, so stderr never hides the CLI's own error and the log cap
    cuts stderr first. A success result is not combined: its "ping succeeded" would lead
    the stderr that explains the failure. In every other case stderr stands alone --
    or, when it is empty, the stream's reason does: the CLI can report its error on stdout
    alone, and the line must not end bare.
    """
    stderr_detail = stderr.strip()[:_DETAIL_MAX_CHARS]
    if code == TIMEOUT_EXIT_CODE:
        return (
            f"exit {code}: {stderr_detail}; last event: {describe_last_event(stdout)}"
        )
    failed_result = (
        outcome.kind == OUTCOME_FAILED and outcome.detail != NO_RESULT_DETAIL
    )
    if failed_result and stderr_detail:
        return f"exit {code}: {outcome.detail}; stderr: {stderr_detail}"
    return f"exit {code}: {stderr_detail or outcome.detail}"


def run_ping(
    config: PrimerConfig,
    state_dir: Path,
    *,
    runner: Runner = run_process,
    base_env: Mapping[str, str] = os.environ,
) -> PingOutcome:
    """Send one ping from *state_dir* and classify it.

    A `limited` stream is trusted even from a failing exit: an account at its limit is
    exactly when the CLI exits with an error yet still reports the reset time.
    """
    argv = build_ping_command(config.claude_path, config.model, config.prompt)
    env = build_ping_env(base_env)
    code, stdout, stderr = runner(argv, PING_TIMEOUT_SECONDS, env, state_dir)
    outcome = parse_ping_output(stdout)
    if outcome.kind == OUTCOME_LIMITED:
        return outcome
    if code != 0:
        detail = _failure_detail(code, stdout, stderr, outcome)
        return PingOutcome(OUTCOME_FAILED, None, detail)
    return outcome
