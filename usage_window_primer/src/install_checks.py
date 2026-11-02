"""The install-time checks of what later ticks use: prompt, model, claude, a last run.

The scheduler runs every tick later, from another working directory and with no one
watching, so a problem either would cause is cheapest to report at install. Each check
raises ValueError with the reason, the error family install already reports as a usage
error. For the CLI, three problems are caught besides one missing from PATH: a file that
cannot be read at all, a Microsoft Store app execution alias, and on Windows a batch
shim. The alias is a 0-byte placeholder under a ``WindowsApps`` folder that only an
interactive shell can resolve, so every scheduled ping through it would fail; a shim runs
through cmd.exe, where the ping's argument quoting does not hold.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PureWindowsPath

from state_store import has_unsafe_character

CLAUDE_EXECUTABLE = "claude"
# The fewest whitespace-separated words a prompt may hold: one word can name a subcommand.
_MIN_PROMPT_WORDS = 2
# The file types Windows runs through cmd.exe, compared case-insensitively.
_BATCH_SUFFIXES = frozenset({".cmd", ".bat"})
# The folder Windows keeps app execution aliases in, compared case-insensitively because
# Windows paths are.
_STORE_ALIAS_FOLDER = "windowsapps"
_ALIAS_SIZE = 0


def _refuse_unsafe_text(option: str, text: str) -> None:
    """Raise ValueError when *text* holds what config.json refuses at every tick."""
    if has_unsafe_character(text):
        raise ValueError(
            f"{option} holds a control character or an undecodable byte, which "
            "config.json cannot carry: every tick would refuse the recorded settings"
        )


def validate_prompt(prompt: str) -> None:
    """Raise ValueError when *prompt* cannot be sent as the ping."""
    if not prompt.strip():
        raise ValueError("--prompt must not be empty or only whitespace")
    if prompt.startswith("-"):
        raise ValueError(
            "--prompt must not start with '-': claude would read it as an option"
        )
    _refuse_unsafe_text("--prompt", prompt)
    # No subcommand name is two words, so a prompt of two or more is always sent as
    # text. Whitespace around one word does not count: "doctor " is still one.
    if len(prompt.split()) < _MIN_PROMPT_WORDS:
        raise ValueError(
            "--prompt must hold at least two words: a single word can name a claude "
            "subcommand, which claude would run instead of sending the prompt"
        )


def validate_model(model: str) -> None:
    """Raise ValueError when *model* is a name config.json cannot record."""
    _refuse_unsafe_text("--model", model)


def validate_last_run(last_run: datetime | None, now: datetime) -> None:
    """Raise ValueError when *last_run* lies after *now*.

    A ping cannot have happened in the future, and recording one would hold every tick
    back until five hours after a moment that has not yet come.
    """
    if last_run is not None and last_run > now:
        raise ValueError(
            f"--last-run {last_run.isoformat()} is in the future; give the moment a "
            "ping was actually sent"
        )


def store_alias_problem(path: str, size: int) -> str | None:
    """Why *path* is an unlaunchable 0-byte Store alias, or None.

    *size* is the file's size in bytes. `PureWindowsPath` splits on both separators, so
    a forward-slash spelling matches too; only a containing folder counts, never the file
    name itself.
    """
    folders = PureWindowsPath(path).parts[:-1]
    if size != _ALIAS_SIZE or not any(
        part.casefold() == _STORE_ALIAS_FOLDER for part in folders
    ):
        return None
    return (
        f"{path} is a 0-byte Microsoft Store app alias, which a scheduled task cannot "
        f"launch; put the folder holding the real {CLAUDE_EXECUTABLE} executable first "
        "on PATH and run install again"
    )


def batch_shim_problem(path: str, platform: str) -> str | None:
    """Why *path* is a batch shim no ping can be run through safely on win32, or None.

    Windows starts a ``.cmd`` or ``.bat`` file through cmd.exe, which parses the command
    line by its own rules: the `subprocess.list2cmdline` quoting that protects the prompt
    and the other arguments for a real executable does not protect them there.
    """
    suffix = PureWindowsPath(path).suffix.casefold()
    if platform != "win32" or suffix not in _BATCH_SUFFIXES:
        return None
    return (
        f"{path} is a batch shim, which runs through cmd.exe and leaves the ping's "
        f"arguments unprotected; put the folder holding {CLAUDE_EXECUTABLE}.exe first "
        "on PATH and run install again"
    )


def locate_claude(
    which: Callable[[str], str | None], *, platform: str = sys.platform
) -> str:
    """The absolute path of the claude CLI *which* finds; ValueError saying why not."""
    found = which(CLAUDE_EXECUTABLE)
    if found is None:
        raise ValueError(
            f"{CLAUDE_EXECUTABLE} is not on PATH; run install from a shell where it is"
        )
    # Absolute, because a relative PATH entry makes `which` answer relative and the
    # scheduler runs the tick from another working directory. Not resolve(): the launcher
    # is typically a symlink into a versioned directory that auto-update replaces, so a
    # resolved target would stop existing after the next update.
    path = os.path.abspath(found)
    try:
        size = Path(path).stat().st_size
    except OSError as exc:
        # The OSError text already ends with the path, so it is not repeated here.
        raise ValueError(f"the {CLAUDE_EXECUTABLE} CLI cannot be read: {exc}") from exc
    problem = store_alias_problem(path, size) or batch_shim_problem(path, platform)
    if problem is not None:
        raise ValueError(problem)
    return path
