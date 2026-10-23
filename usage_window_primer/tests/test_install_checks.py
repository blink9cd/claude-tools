"""The install-time checks: the ping prompt, and the claude CLI an install records.

`PromptTest`, `ModelTest` and `StoreAliasTest` are pure. `LocateClaudeTest` stats real
files in a temporary directory, because the size it checks is the file system's answer, not one a
test could fake.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TOOL_ROOT / "src"))

from install_checks import (  # noqa: E402 - import follows the sys.path bootstrap above
    CLAUDE_EXECUTABLE,
    locate_claude,
    store_alias_problem,
    validate_model,
    validate_prompt,
)

_ALIAS = "C:\\fake\\AppData\\Local\\Microsoft\\WindowsApps\\claude.exe"
_REAL_SIZE = 1024
# Built with chr(): an editing tool can decode a typed escape into the raw character.
_NUL, _ESC, _LONE_SURROGATE = chr(0), chr(0x1B), chr(0xDC80)


class PromptTest(unittest.TestCase):
    """A prompt claude can receive as its argument passes; any other is refused."""

    def test_an_ordinary_prompt_passes(self) -> None:
        validate_prompt("Reply with the single word: ok")

    def test_an_empty_or_blank_prompt_is_refused(self) -> None:
        for prompt in ("", "  \t"):
            with (
                self.subTest(prompt=prompt),
                self.assertRaisesRegex(ValueError, "must not be empty"),
            ):
                validate_prompt(prompt)

    def test_a_prompt_read_as_an_option_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not start with '-'"):
            validate_prompt("-x")

    def test_a_one_word_prompt_is_refused(self) -> None:
        # Regression: a prompt with no whitespace can name a claude subcommand, which
        # the CLI would run instead of sending it as the message. Whitespace around a
        # single word does not make it two ("doctor " passed the first fix).
        for prompt in ("update", "mcp", "ok", "doctor ", " doctor", "  mcp  "):
            with (
                self.subTest(prompt=prompt),
                self.assertRaisesRegex(ValueError, "two words"),
            ):
                validate_prompt(prompt)

    def test_a_prompt_with_a_control_character_is_refused(self) -> None:
        # Regression: config.json refuses one at every tick, so install must too.
        for character in (_ESC, _NUL, _LONE_SURROGATE):
            with (
                self.subTest(character=hex(ord(character))),
                self.assertRaisesRegex(ValueError, "--prompt holds a control"),
            ):
                validate_prompt(f"hello{character} world")

    def test_a_prompt_split_only_by_a_tab_is_refused(self) -> None:
        # Regression: install accepted "Reply<TAB>ok" as two words, but a tab is a
        # control character, which config.json refuses, so every tick then exited 2.
        with self.assertRaisesRegex(ValueError, "--prompt holds a control"):
            validate_prompt("Reply\tok")


class ModelTest(unittest.TestCase):
    """A model name config.json can carry passes; one it would refuse does not."""

    def test_an_ordinary_model_passes(self) -> None:
        validate_model("haiku")

    def test_a_model_with_a_control_character_is_refused(self) -> None:
        for character in (_ESC, _NUL, _LONE_SURROGATE):
            with (
                self.subTest(character=hex(ord(character))),
                self.assertRaisesRegex(ValueError, "--model holds a control"),
            ):
                validate_model(f"haiku{character}[2J")


class StoreAliasTest(unittest.TestCase):
    """A 0-byte file under a WindowsApps folder is an alias; anything else is not."""

    def test_a_zero_byte_file_under_windows_apps_is_an_alias(self) -> None:
        problem = store_alias_problem(_ALIAS, 0)
        assert problem is not None
        self.assertIn("Store", problem)
        self.assertIn(_ALIAS, problem)

    def test_the_folder_matches_in_any_case(self) -> None:
        self.assertIsNotNone(
            store_alias_problem(_ALIAS.replace("WindowsApps", "windowsapps"), 0)
        )

    def test_a_forward_slash_path_matches(self) -> None:
        self.assertIsNotNone(store_alias_problem(_ALIAS.replace("\\", "/"), 0))

    def test_a_file_with_content_is_not_an_alias(self) -> None:
        self.assertIsNone(store_alias_problem(_ALIAS, _REAL_SIZE))

    def test_a_zero_byte_file_elsewhere_is_not_an_alias(self) -> None:
        self.assertIsNone(store_alias_problem("C:\\fake\\bin\\claude.exe", 0))

    def test_a_file_named_like_the_folder_is_not_an_alias(self) -> None:
        # Only a containing folder counts: the alias lives INSIDE WindowsApps.
        self.assertIsNone(store_alias_problem("C:\\fake\\bin\\WindowsApps", 0))


class LocateClaudeTest(unittest.TestCase):
    """The absolute path install records, or a ValueError naming why there is none."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _file(self, relative: str, content: bytes) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_a_real_cli_is_returned_absolute(self) -> None:
        # The scheduler runs the tick from another working directory, so a relative
        # PATH entry must not leave a relative path behind.
        self._file("relative/bin/claude", b"#!/bin/sh\n")
        asked: list[str] = []

        def which(name: str) -> str | None:
            asked.append(name)
            return "relative/bin/claude"

        with contextlib.chdir(self.root):
            located = locate_claude(which)
        self.assertEqual(asked, [CLAUDE_EXECUTABLE])
        self.assertTrue(os.path.isabs(located))
        self.assertEqual(located, str(self.root / "relative" / "bin" / "claude"))

    def test_a_cli_not_on_path_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "claude is not on PATH"):
            locate_claude(lambda _name: None)

    def test_a_cli_that_cannot_be_read_is_refused(self) -> None:
        missing = str(self.root / "gone" / "claude")
        with self.assertRaisesRegex(ValueError, "cannot be read") as caught:
            locate_claude(lambda _name: missing)
        self.assertIsInstance(caught.exception.__cause__, OSError)

    def test_a_store_alias_is_refused(self) -> None:
        alias = self._file("WindowsApps/claude.exe", b"")
        with self.assertRaisesRegex(ValueError, "Store"):
            locate_claude(lambda _name: str(alias))

    def test_a_batch_shim_is_refused_on_windows(self) -> None:
        # Regression: a .cmd or .bat claude runs through cmd.exe, whose parsing the
        # list2cmdline quoting of the prompt does not protect, so it was recorded as is.
        for name in ("claude.cmd", "claude.bat", "claude.CMD", "claude.Bat"):
            found = str(self._file(f"npm/{name}", b"@echo off\r\n"))

            def which(_name: str, path: str = found) -> str | None:
                return path

            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "claude.exe"),
            ):
                locate_claude(which, platform="win32")

    def test_a_batch_shim_is_kept_off_windows(self) -> None:
        # Off Windows a .cmd suffix means nothing to the shell-free launch.
        shim = self._file("npm/claude.cmd", b"#!/bin/sh\n")
        self.assertEqual(
            locate_claude(lambda _name: str(shim), platform="darwin"), str(shim)
        )

    def test_an_exe_is_kept_on_windows(self) -> None:
        exe = self._file("bin/claude.exe", b"MZ")
        self.assertEqual(
            locate_claude(lambda _name: str(exe), platform="win32"), str(exe)
        )


if __name__ == "__main__":
    unittest.main()
