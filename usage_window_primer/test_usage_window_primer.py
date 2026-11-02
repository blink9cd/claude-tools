"""The usage_window_primer tool's top-level test module: the tests/ aggregator.

This file is the folder's single entry point and coverage unit, and `load_tests` below
folds in every grouped suite under `tests/`, which is what makes each of them part of the
real run. `GroupedSuiteDiscoveryTest` fails loudly if that hook is ever lost.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

_HERE = Path(__file__).resolve().parent
_SUITE_DIR = _HERE / "tests"
sys.path.insert(0, str(_HERE / "src"))

from ping_output import (  # noqa: E402 - import follows the sys.path bootstrap above
    OUTCOME_FAILED,
    OUTCOME_LIMITED,
    OUTCOME_OK,
    describe_last_event,
    parse_ping_output,
)
from state_store import EMPTY_STATE  # noqa: E402 - same bootstrap
from window_rules import apply_outcome, trusted_window_end  # noqa: E402 - same bootstrap

# The determinism convention: a profile registered at import, never
# an `@settings` decorator, which a fuzzing run applying its own settings to every
# property could not override.
# deadline=None: a per-example wall-clock deadline measures machine load, not
# correctness, so a busy machine would report a passing property as flaky.
# too_slow is suppressed for the same reason: on a cold .hypothesis cache the
# first draw also builds Hypothesis's unicode tables and scans the local modules
# for constants, which under load outlasts the limit that check allows even
# without a deadline.
settings.register_profile(
    "fuzz_gate_deterministic",
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("fuzz_gate_deterministic")

# Restated, not derived: a floor computed from discovery would compare discovery against
# itself and pass for an empty suite. A change that adds a grouped file adds its row.
_MIN_TESTS_BY_FILE: dict[str, int] = {
    "test_process_run.py": 15,
    "test_ping_output.py": 27,
    "test_state_store.py": 17,
    "test_window_rules.py": 15,
    "test_ping_runner.py": 19,
    "test_launch_agent.py": 22,
    "test_usage_window_primer_tick.py": 23,
    "test_usage_window_primer_status.py": 14,
    "test_usage_window_primer_install.py": 20,
    "test_usage_window_primer_help.py": 2,
    "test_scheduler.py": 13,
    "test_task_agent.py": 31,
    "test_headless_streams.py": 5,
    "test_install_checks.py": 18,
    "test_state_dir_guard.py": 18,
    "test_samples.py": 2,
    "test_usage_window_primer_env_declared.py": 3,
    "test_bootstrap_bound.py": 1,
    # Counted in full even where the argv route test is skipped, like the live tests below.
    "test_failure_alert.py": 22,
    # Counted in full even where the contract test is skipped, like the live ping below.
    "test_launchctl_contract.py": 5,
    # Always 1: the live class is loaded (and counted) even when it is later skipped.
    "test_live_ping.py": 1,
    "test_live_notify.py": 1,
    "test_task_scheduler_live.py": 1,
}


def _grouped_suite() -> unittest.TestSuite:
    """Every test in tests/, discovered by walking the directory."""
    return unittest.TestLoader().discover(
        str(_SUITE_DIR), pattern="test_*.py", top_level_dir=str(_SUITE_DIR)
    )


def _tests_by_file(suite: unittest.TestSuite) -> dict[str, int]:
    """Flatten a nested suite into a per-file test count."""
    counts: dict[str, int] = {}
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            for name, count in _tests_by_file(item).items():
                counts[name] = counts.get(name, 0) + count
        else:
            name = f"{type(item).__module__}.py"
            counts[name] = counts.get(name, 0) + 1
    return counts


class GroupedSuiteDiscoveryTest(unittest.TestCase):
    """This module's real load must include every grouped file at its floor."""

    def test_load_tests_folds_in_every_grouped_file(self) -> None:
        # Loads this module the way unittest.main does, so deleting load_tests fails
        # here instead of silently dropping every grouped suite from the coverage run.
        loaded = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
        counts = _tests_by_file(loaded)
        for filename, floor in _MIN_TESTS_BY_FILE.items():
            with self.subTest(filename=filename):
                self.assertGreaterEqual(counts.get(filename, 0), floor)


_JSON_SCALARS = (
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=12)
)
_JSON_VALUES = st.recursive(
    _JSON_SCALARS,
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(st.text(max_size=12), children, max_size=4)
    ),
    max_leaves=12,
)
# 2100-01-01T00:00:00Z, an upper bound on a window end a real ping could report.
_LAST_PLAUSIBLE_EPOCH = 4_102_444_800
# Integers in this range lie far past datetime's year-9999 limit; the ceiling is the
# example suite's own overflow case.
_OVERFLOWING_EPOCH_FLOOR = 10**18
_OVERFLOWING_EPOCH_CEILING = 10**400

# Every shape a window end can take on the wire: plausible epochs, arbitrary and
# overflowing integers, floats (inf and NaN included), JSON booleans, and strings.
_EPOCHS = (
    st.integers(min_value=0, max_value=_LAST_PLAUSIBLE_EPOCH)
    | st.integers()
    | st.integers(
        min_value=_OVERFLOWING_EPOCH_FLOOR, max_value=_OVERFLOWING_EPOCH_CEILING
    )
    | st.floats()
    | st.booleans()
    | st.text(max_size=12)
)
_WINDOW = st.fixed_dictionaries(
    {"resetsAt": _EPOCHS}, optional={"utilization": st.floats()}
)
_STATUSES = st.sampled_from(["allowed", "allowed_warning", "rejected"])
_WINDOW_TYPES = st.sampled_from(["five_hour", "seven_day"])
# The event shapes the parser reads. Values are drawn from the literal strings the parser
# compares against, because arbitrary text is never long enough to spell them. A
# rate_limit_info either carries unifiedWindows, as a real ping sends it (its five_hour
# window possibly junk), or only the top-level fields, whose window type may be another.
_RATE_LIMIT_INFOS = st.fixed_dictionaries(
    {
        "status": _STATUSES,
        "unifiedWindows": st.fixed_dictionaries(
            {"five_hour": _WINDOW | _JSON_VALUES}, optional={"seven_day": _WINDOW}
        ),
    },
    optional={"rateLimitType": _WINDOW_TYPES, "resetsAt": _EPOCHS},
) | st.fixed_dictionaries(
    {"status": _STATUSES, "rateLimitType": _WINDOW_TYPES, "resetsAt": _EPOCHS}
)
_RATE_LIMIT_EVENTS = st.fixed_dictionaries(
    {"type": st.just("rate_limit_event"), "rate_limit_info": _RATE_LIMIT_INFOS}
)
_RESULT_EVENTS = st.fixed_dictionaries(
    {
        "type": st.just("result"),
        "subtype": st.sampled_from(["success", "error_during_execution"]),
        "is_error": st.booleans(),
    }
)
# The same fields holding arbitrary JSON, under a real or an arbitrary type, so the
# refusal paths get input too: a non-object rate_limit_info, a missing or non-string
# status, a non-object unifiedWindows, a result with no subtype or a non-boolean is_error,
# and a result message that is missing, blank or not a string.
_INFO_FIELDS = st.sampled_from(
    ["status", "rateLimitType", "resetsAt", "unifiedWindows"]
)
_MISSHAPEN_EVENTS = st.fixed_dictionaries(
    {"type": st.sampled_from(["rate_limit_event", "result", "system"]) | _JSON_VALUES},
    optional={
        "rate_limit_info": _JSON_VALUES
        | st.dictionaries(_INFO_FIELDS | st.text(max_size=8), _JSON_VALUES, max_size=5),
        "subtype": _JSON_VALUES,
        "is_error": _JSON_VALUES,
        "result": _JSON_VALUES,
    },
)
_ARBITRARY_OBJECTS = st.dictionaries(st.text(max_size=8), _JSON_VALUES, max_size=6)
_EVENTS = _RATE_LIMIT_EVENTS | _RESULT_EVENTS | _MISSHAPEN_EVENTS | _ARBITRARY_OBJECTS
# A stream is raw text, an arbitrary sequence of events and other JSON values (lines that
# parse but are not objects), or the measured ping shape: one rate_limit_event followed
# by one result.
_EVENT_SEQUENCES = st.lists(_EVENTS | _JSON_VALUES, max_size=5) | st.tuples(
    _RATE_LIMIT_EVENTS, _RESULT_EVENTS
).map(list)
_STREAMS = st.text(max_size=400) | _EVENT_SEQUENCES.map(
    lambda events: "\n".join(json.dumps(event) for event in events)
)
# A stream known to classify as limited. It runs on every test run, so the
# limited-implies-a-window-end assertion is proven to execute rather than left to chance.
_LIMITED_STREAM = "\n".join(
    json.dumps(event)
    for event in (
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "rejected",
                "unifiedWindows": {"five_hour": {"resetsAt": _LAST_PLAUSIBLE_EPOCH}},
            },
        },
        {"type": "result", "subtype": "error_during_execution", "is_error": True},
    )
)
# What a killed ping leaves: a stream cut at any point, usually partway through a line.
_CUT_STREAMS = st.tuples(_STREAMS, st.integers(min_value=0)).map(
    lambda pair: pair[0][: pair[1]]
)
# The longest label describe_last_event may return, restated rather than imported so the
# property pins the bound instead of agreeing with whatever the module says it is.
_EVENT_LABEL_MAX_CHARS = 60
# An event whose label would run far past that bound, so the cap is proven to execute.
_OVERLONG_EVENT = json.dumps({"type": "t" * 300, "subtype": "s" * 300})
# The bound on a recorded window end, restated for the same reason: a window lasts five
# hours, and the slack allows for this machine's clock disagreeing with the server's.
_WINDOW_LENGTH = timedelta(hours=5)
_WINDOW_END_SLACK = timedelta(minutes=15)
# Ping moments far from datetime's limits, so the bound itself is representable.
_PING_MOMENTS = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 1, 1),
    timezones=st.just(UTC),
)
# The measured ping shape alone, so most draws carry a rate_limit_event and a result.
_WINDOWED_STREAMS = st.tuples(_RATE_LIMIT_EVENTS, _RESULT_EVENTS).map(
    lambda events: "\n".join(json.dumps(event) for event in events)
)
# The stream that froze the tool: a success reporting a window that ends in year 9999.
_FAR_FUTURE_STREAM = "\n".join(
    json.dumps(event)
    for event in (
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed",
                "unifiedWindows": {"five_hour": {"resetsAt": 253402300000}},
            },
        },
        {"type": "result", "subtype": "success", "is_error": False},
    )
)
_FAR_FUTURE_PINGED_AT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class ParsePingOutputPropertyTest(unittest.TestCase):
    """The never-raises contract over arbitrary text and generated event streams."""

    @given(_STREAMS)
    @example(_LIMITED_STREAM)
    def test_fuzz_parse_ping_output(self, text: str) -> None:
        outcome = parse_ping_output(text)
        self.assertIn(outcome.kind, (OUTCOME_OK, OUTCOME_LIMITED, OUTCOME_FAILED))
        if outcome.kind == OUTCOME_LIMITED:
            self.assertIsNotNone(outcome.resets_at)


class DescribeLastEventPropertyTest(unittest.TestCase):
    """Whatever a killed ping wrote, the log gets a short, non-empty label for it."""

    @given(_STREAMS | _CUT_STREAMS)
    @example(_OVERLONG_EVENT)
    def test_fuzz_describe_last_event(self, text: str) -> None:
        label = describe_last_event(text)
        self.assertIsInstance(label, str)
        self.assertTrue(label)
        self.assertLessEqual(len(label), _EVENT_LABEL_MAX_CHARS)


class TrustedWindowEndPropertyTest(unittest.TestCase):
    """No reported resetsAt, however wild, records a window end a real window cannot have."""

    @given(_STREAMS | _WINDOWED_STREAMS, _PING_MOMENTS)
    @example(_FAR_FUTURE_STREAM, _FAR_FUTURE_PINGED_AT)
    def test_fuzz_trusted_window_end(self, text: str, pinged_at: datetime) -> None:
        outcome = trusted_window_end(parse_ping_output(text), pinged_at)
        state = apply_outcome(EMPTY_STATE, outcome, pinged_at)
        if outcome.kind == OUTCOME_FAILED:
            self.assertIsNone(state.window_ends_at)
            return
        assert state.window_ends_at is not None
        self.assertGreater(state.window_ends_at, pinged_at)
        self.assertLessEqual(
            state.window_ends_at, pinged_at + _WINDOW_LENGTH + _WINDOW_END_SLACK
        )


def load_tests(
    _loader: unittest.TestLoader, tests: unittest.TestSuite, _pattern: str | None
) -> unittest.TestSuite:
    """unittest's hook: fold the grouped suite in beside this file's own tests."""
    return unittest.TestSuite([tests, _grouped_suite()])


if __name__ == "__main__":
    unittest.main()
