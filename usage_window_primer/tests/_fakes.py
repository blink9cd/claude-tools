"""Test doubles shared by the grouped suites.

The stream builders produce ping output in the shape a real headless ping was measured
to emit, minus every request and session identifier.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

RunResult = tuple[int, str, str]

# 2026-01-01T18:00:00Z, a fixed window end every suite can share.
RESET_EPOCH = 1767290400
RESET_AT = datetime(2026, 1, 1, 18, 0, tzinfo=UTC)

SYSTEM_INIT = json.dumps({"type": "system", "subtype": "init"})
SUCCESS_RESULT = json.dumps(
    {"type": "result", "subtype": "success", "is_error": False, "result": "ok"}
)
ERROR_RESULT = json.dumps(
    {"type": "result", "subtype": "error_during_execution", "is_error": True}
)
# The measured shape of an API error (an expired login among them): the subtype still reads
# "success" and only is_error marks the failure. The wording is illustrative, not captured.
CLI_ERROR_RESULT = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "result": "OAuth session expired",
    }
)


def rate_limit_line(status: str, resets_at: object) -> str:
    """A rate_limit_event line whose five-hour window ends at *resets_at*."""
    return json.dumps(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": status,
                "resetsAt": resets_at,
                "rateLimitType": "five_hour",
                "unifiedWindows": {
                    "five_hour": {"utilization": 0.07, "resetsAt": resets_at},
                    "seven_day": {"utilization": 0.01, "resetsAt": 1767830400},
                },
            },
        }
    )


def stream(*lines: str) -> str:
    """Join event lines the way the CLI writes them: one JSON object per line."""
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class Call:
    """One command a fake runner received."""

    argv: list[str]
    timeout: float
    env: Mapping[str, str] | None
    cwd: Path | None


@dataclass
class ScriptedRunner:
    """A `process_run.Runner` that answers from queues keyed on a command's first two words.

    The last answer in a queue repeats for every later call, so a steady state needs one
    entry. A command with no queue raises KeyError, failing the test loudly instead of
    answering something plausible.
    """

    answers: dict[tuple[str, str], list[RunResult]]
    calls: list[Call] = field(default_factory=list)

    def __call__(
        self,
        argv: list[str],
        timeout: float,
        env: Mapping[str, str] | None,
        cwd: Path | None,
    ) -> RunResult:
        """Record the call and return the next scripted answer for its command."""
        self.calls.append(Call(list(argv), timeout, env, cwd))
        queue = self.answers[(argv[0], argv[1])]
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]

    def commands(self) -> list[list[str]]:
        """The argument vectors received, in order."""
        return [call.argv for call in self.calls]
