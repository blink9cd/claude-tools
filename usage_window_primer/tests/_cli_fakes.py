"""CLI test doubles: `Seams` wired to a scripted runner, a fixed clock and fake paths."""

from __future__ import annotations

import io
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TOOL_ROOT))
sys.path.insert(0, str(_TOOL_ROOT / "src"))

from _fakes import RunResult, ScriptedRunner  # noqa: E402 - import follows the sys.path bootstrap above
from usage_window_primer import DEFAULT_MODEL, DEFAULT_PROMPT, Seams  # noqa: E402 - same bootstrap
from state_store import PrimerConfig, save_config  # noqa: E402 - same bootstrap

# Four hours before _fakes.RESET_AT, so a ping at this moment reporting that window end
# reports one a real window can have, and the tick believes it.
FIXED_NOW = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
FAKE_CLAUDE = "/opt/fake/bin/claude"
FAKE_PYTHON = "/opt/fake/bin/python3"
# Owner-only, the mode install gives a state directory it creates.
PRIVATE_DIR_MODE = 0o700


def make_seams(
    home: Path,
    answers: dict[tuple[str, str], list[RunResult]],
    *,
    now: datetime = FIXED_NOW,
    claude: str | None = FAKE_CLAUDE,
    platform: str = "darwin",
    environ: Mapping[str, str] | None = None,
    python: str = FAKE_PYTHON,
) -> tuple[Seams, ScriptedRunner, io.StringIO, io.StringIO]:
    """Seams that touch nothing real, plus the runner and streams to assert on.

    *platform* defaults to darwin whatever the host is, so a suite reads the same on
    every machine; the state-dir guard alone ignores it and follows the real host, since
    it stats the real filesystem. *environ* adds variables to, or overrides, the fake
    PATH-only one.
    *python* is the interpreter install records; a test whose backend stats the folder
    around it passes a real one.
    """
    runner = ScriptedRunner(answers)
    out, err = io.StringIO(), io.StringIO()
    seams = Seams(
        run=runner,
        now=lambda: now,
        which=lambda _name: claude,
        home=home,
        environ={"PATH": "/usr/bin", **(environ or {})},
        python=python,
        script="/opt/fake/usage_window_primer.py",
        out=out,
        err=err,
        platform=platform,
    )
    return seams, runner, out, err


def install_config(state_dir: Path, *, start: datetime | None = None) -> None:
    """Record the config an install would have, without loading any agent.

    A missing state directory is made private, as install makes it, so a tick's
    ownership and mode check passes whatever the umask of the account running the tests.
    """
    state_dir.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    config = PrimerConfig(start, DEFAULT_MODEL, DEFAULT_PROMPT, 5, FAKE_CLAUDE)
    save_config(state_dir, config)
