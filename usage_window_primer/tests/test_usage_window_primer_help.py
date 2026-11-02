"""`--help` keeps a required option free of a default note on the 3.12 floor.

Below 3.13, argparse's ArgumentDefaultsHelpFormatter stamps "(default: None)" onto a
REQUIRED option, which here is `--mode`. On 3.13 and later that fix is in, so rendering
the real help there proves nothing: it passes with the shared formatter removed
("The version trap" in help_formatter/help_formatter.md). These tests therefore
pin the binding to the shared formatter, and render the real parser over a stand-in for
old argparse, so either one catches the stock formatter coming back.
"""

from __future__ import annotations

import argparse
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from usage_window_primer import build_parser  # noqa: E402 - import follows the sys.path bootstrap above

_DEFAULT_NOTE = " (default: %(default)s)"
_STATE_DIR = Path("/opt/fake/state")


def _legacy_help_string(
    _self: argparse.HelpFormatter, action: argparse.Action
) -> str | None:
    """Stand in for argparse < 3.13: note the default on every action that has one.

    Required or not, which is the bug. A suppressed default (-h's) gets no note, as in
    old argparse: the help expansion drops it, so a note naming it cannot be filled.
    """
    if action.default is argparse.SUPPRESS:
        return action.help
    return (action.help or "") + _DEFAULT_NOTE


def _option_help_line(text: str, option: str) -> str:
    """The rendered help entry for *option*, its wrapped lines joined into one."""
    # An entry runs from its own "  --option" line to the next option's, or the end.
    found = re.search(rf"^  {option} .*?(?=^  -|\Z)", text, re.MULTILINE | re.DOTALL)
    assert found is not None, f"{option} is missing from the help"
    return " ".join(found.group(0).split())


class RequiredOptionHelpTest(unittest.TestCase):
    """A required option never advertises a default, whatever argparse does below it."""

    def test_the_formatter_is_the_shared_one(self) -> None:
        # Imported here, not at the top: help_formatter reaches sys.path only through
        # usage_window_primer's own bootstrap, and a module-level import would turn a lost binding
        # into an import error that hides the rendering test below.
        import help_formatter

        parser = build_parser(_STATE_DIR)
        self.assertIsInstance(
            parser.formatter_class(prog="x"),
            help_formatter.UnsetHidingFormatter,
        )

    def test_an_option_left_unset_shows_no_default(self) -> None:
        # --start, --last-run, and --ping-output-file default to None, which nobody can
        # type; "(default: None)" would only say the option was not given.
        text = build_parser(_STATE_DIR).format_help()
        for option in ("--start", "--last-run", "--ping-output-file"):
            with self.subTest(option=option):
                self.assertNotIn("(default:", _option_help_line(text, option))
        self.assertIn("(default: 5)", _option_help_line(text, "--poll-minutes"))

    def test_mode_shows_no_default_over_legacy_argparse(self) -> None:
        # Patching the stock hook reproduces the old unconditional append on any
        # interpreter, so the shared formatter's override is what keeps it off --mode.
        with mock.patch.object(
            argparse.ArgumentDefaultsHelpFormatter,
            "_get_help_string",
            _legacy_help_string,
        ):
            text = build_parser(_STATE_DIR).format_help()
        self.assertNotIn("(default:", _option_help_line(text, "--mode"))
        self.assertIn("(default: haiku)", _option_help_line(text, "--model"))


if __name__ == "__main__":
    unittest.main()
