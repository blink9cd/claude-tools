"""The state directory's guard: its name, its owner and mode, and the CLI refusing on both.

Every tick runs the `claude_path` that config.json names, so a state directory another
account can write to is a way to run code as this user. The unit tests stat real
directories in a temporary folder; the owner check takes an injected effective uid, so a
foreign owner is reached without a second account. A test that sets mode bits or needs a
POSIX error runs only on a POSIX host: Windows keeps no mode bits for chmod to set (every
directory reads as 0o777 there), and the guard skips a non-POSIX host by design.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _cli_fakes import FAKE_CLAUDE, install_config, make_seams  # noqa: E402 - import follows the sys.path bootstrap above
from _fakes import SUCCESS_RESULT, stream  # noqa: E402 - same bootstrap
from usage_window_primer import EXIT_USAGE, main  # noqa: E402 - same bootstrap
from state_dir_guard import (  # noqa: E402 - same bootstrap
    PRIVATE_DIR_MODE,
    DefaultStateDir,
    effective_uid,
    ensure_private_dir,
    state_dir_problem,
    state_dir_text_problem,
)
from state_store import CONFIG_FILENAME, STATE_FILENAME  # noqa: E402 - same bootstrap

# os.geteuid exists only off Windows, and the gate type-checks for win32 too.
_GETEUID = getattr(os, "geteuid", None)
# Built with chr(): an editing tool can decode a typed escape into the raw character.
_NUL, _SOH, _ESC, _DEL, _NEL = chr(0), chr(1), chr(0x1B), chr(0x7F), chr(0x85)
_LONE_SURROGATE = chr(0xDC80)
_POSIX_HOST = os.name == "posix"
_NEEDS_POSIX = "needs POSIX mode bits and errors; the guard skips a non-POSIX host"
# Read and search, no write: mkdir inside it fails with a permission error.
_READ_ONLY_DIR_MODE = 0o500
# Root writes into a folder whatever its mode bits say.
_RUNNING_AS_ROOT = _GETEUID is not None and _GETEUID() == 0


class StateDirTextTest(unittest.TestCase):
    """A state directory name the plist, the task XML and the OS can all carry."""

    def test_an_ordinary_path_passes(self) -> None:
        for text in ("/opt/fake/state", "relative/dir", "with space/é"):
            with self.subTest(text=text):
                self.assertIsNone(state_dir_text_problem(text))

    def test_a_control_character_or_a_lone_surrogate_is_refused(self) -> None:
        # Regression: a NUL crashed every mode's path resolution, and any other control
        # character crashed the plist write after config.json had been recorded. An
        # undecodable byte in argv arrives as a lone surrogate, which no file can encode.
        for character in (_NUL, _SOH, "\t", "\n", _ESC, _DEL, _NEL, _LONE_SURROGATE):
            with self.subTest(character=hex(ord(character))):
                problem = state_dir_text_problem(f"/opt/fake/a{character}b")
                assert problem is not None
                self.assertIn("--state-dir", problem)
                self.assertTrue(problem.isprintable(), repr(problem))


class StateDirProblemTest(unittest.TestCase):
    """Owned by this user, not writable by any other, and a directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir(mode=PRIVATE_DIR_MODE)
        self.owner = self.state_dir.stat().st_uid

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _problem(self, *, posix: bool = True, euid: int | None = None) -> str | None:
        uid = self.owner if euid is None else euid
        return state_dir_problem(self.state_dir, posix=posix, euid=lambda: uid)

    @unittest.skipUnless(_POSIX_HOST, _NEEDS_POSIX)
    def test_a_private_or_shared_read_only_dir_passes(self) -> None:
        for mode in (0o700, 0o755, 0o750):
            with self.subTest(mode=oct(mode)):
                self.state_dir.chmod(mode)
                self.assertIsNone(self._problem())

    @unittest.skipUnless(_POSIX_HOST, _NEEDS_POSIX)
    def test_a_dir_others_can_write_is_refused(self) -> None:
        for mode in (0o720, 0o702, 0o777, 0o1777):
            with self.subTest(mode=oct(mode)):
                self.state_dir.chmod(mode)
                problem = self._problem()
                assert problem is not None
                self.assertIn("writable by other users", problem)
                self.assertIn(oct(mode & 0o777), problem)

    def test_a_dir_another_user_owns_is_refused(self) -> None:
        problem = self._problem(euid=self.owner + 1)
        assert problem is not None
        self.assertIn("not owned by", problem)

    def test_a_non_posix_host_skips_the_check(self) -> None:
        # Windows has no POSIX owner or mode bits to read; its ACLs are another model.
        self.state_dir.chmod(0o777)
        self.assertIsNone(self._problem(posix=False, euid=self.owner + 1))

    def test_a_missing_dir_is_left_to_the_caller(self) -> None:
        # A tick then reports "no install"; install creates the directory before asking.
        self.state_dir.rmdir()
        self.assertIsNone(self._problem())

    def test_a_file_in_its_place_is_refused(self) -> None:
        self.state_dir.rmdir()
        self.state_dir.write_bytes(b"")
        problem = self._problem()
        assert problem is not None
        self.assertIn("not a directory", problem)

    @unittest.skipUnless(_POSIX_HOST, _NEEDS_POSIX)
    def test_a_path_that_cannot_be_read_is_refused(self) -> None:
        # Its parent is a file, so the stat fails with an error other than not-found.
        blocker = self.root / "blocker"
        blocker.write_bytes(b"")
        problem = state_dir_problem(
            blocker / "state", posix=True, euid=lambda: self.owner
        )
        assert problem is not None
        self.assertIn("cannot be read", problem)


class EnsurePrivateDirTest(unittest.TestCase):
    """install creates a missing state directory owner-only, and leaves one that exists."""

    @unittest.skipUnless(_POSIX_HOST, _NEEDS_POSIX)
    def test_a_missing_dir_and_its_parents_are_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "a" / "state"
            self.assertIsNone(ensure_private_dir(state_dir))
            self.assertEqual(stat.S_IMODE(state_dir.stat().st_mode), PRIVATE_DIR_MODE)

    def test_a_file_in_its_place_is_left_for_the_guard(self) -> None:
        # The guard then reports it as not a directory, with exit 2, not a traceback.
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.write_bytes(b"")
            self.assertIsNone(ensure_private_dir(state_dir))
            self.assertTrue(state_dir.is_file())

    @unittest.skipUnless(_POSIX_HOST, _NEEDS_POSIX)
    def test_a_dir_that_cannot_be_created_is_reported(self) -> None:
        # Regression: any OSError but FileExistsError escaped as a traceback (an
        # undecodable name gave EILSEQ from mkdir); a file as a parent gives ENOTDIR.
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocker"
            blocker.write_bytes(b"")
            problem = ensure_private_dir(blocker / "state")
            assert problem is not None
            self.assertIn("cannot be created", problem)

    @unittest.skipUnless(_POSIX_HOST, _NEEDS_POSIX)
    def test_an_existing_dir_keeps_its_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            state_dir.chmod(0o755)
            ensure_private_dir(state_dir)
            self.assertEqual(stat.S_IMODE(state_dir.stat().st_mode), 0o755)


class SeamDefaultsTest(unittest.TestCase):
    """The real effective uid, and the marker for a state directory nobody named."""

    @unittest.skipIf(_GETEUID is None, "no effective uid on Windows")
    def test_effective_uid_is_the_process_s(self) -> None:
        assert _GETEUID is not None
        self.assertEqual(effective_uid(), _GETEUID())

    def test_the_default_marker_prints_as_its_path(self) -> None:
        # --help prints it as the option's default, so it must read as the real one.
        self.assertEqual(
            str(DefaultStateDir(Path("/opt/fake/state"))), "/opt/fake/state"
        )


class CliGuardTest(unittest.TestCase):
    """install and tick refuse an unsafe state directory before touching anything in it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()
        self.state_dir = self.home / "state"
        claude = self.home / "bin" / "claude"
        claude.parent.mkdir()
        claude.write_bytes(b"#!/bin/sh\n")
        self.claude = str(claude)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _tick(
        self, *, platform: str = "darwin", euid: int | None = None
    ) -> tuple[int, int, str]:
        ping = (0, stream(SUCCESS_RESULT), "")
        seams, runner, _out, err = make_seams(
            self.home, {(FAKE_CLAUDE, "-p"): [ping]}, platform=platform
        )
        if euid is not None:
            seams = dataclasses.replace(seams, euid=lambda: euid)
        argv = ["--mode", "tick", "--state-dir", str(self.state_dir)]
        return main(argv, seams=seams), len(runner.calls), err.getvalue()

    @unittest.skipUnless(_POSIX_HOST, _NEEDS_POSIX)
    def test_a_tick_refuses_a_state_dir_others_can_write(self) -> None:
        install_config(self.state_dir)
        self.state_dir.chmod(0o777)
        code, calls, err = self._tick()
        self.assertEqual((code, calls), (EXIT_USAGE, 0))
        self.assertIn("writable by other users", err)
        self.assertFalse((self.state_dir / STATE_FILENAME).exists())

    @unittest.skipUnless(_POSIX_HOST, _NEEDS_POSIX)
    def test_a_tick_refuses_a_state_dir_another_user_owns(self) -> None:
        install_config(self.state_dir)
        code, calls, err = self._tick(euid=self.state_dir.stat().st_uid + 1)
        self.assertEqual((code, calls), (EXIT_USAGE, 0))
        self.assertIn("not owned by", err)

    @unittest.skipUnless(_POSIX_HOST, _NEEDS_POSIX)
    def test_the_real_host_decides_not_the_faked_platform(self) -> None:
        # Regression: the guard followed the platform seam, which the CLI tests fake as
        # darwin on every host. On Windows that ran the mode check against a filesystem
        # that reads every directory as 0o777, and refused every faked-darwin tick. The
        # check reads the real filesystem, so only the real host's kind may decide it.
        install_config(self.state_dir)
        self.state_dir.chmod(0o777)
        code, calls, err = self._tick(platform="win32")
        self.assertEqual((code, calls), (EXIT_USAGE, 0))
        self.assertIn("writable by other users", err)

    @unittest.skipUnless(_POSIX_HOST, _NEEDS_POSIX)
    @unittest.skipIf(
        _RUNNING_AS_ROOT, "root creates a folder whatever the mode bits say"
    )
    def test_install_reports_a_state_dir_it_cannot_create(self) -> None:
        # A parent nobody may write: the state read finds nothing, then mkdir is refused.
        locked = self.home / "locked"
        locked.mkdir(mode=_READ_ONLY_DIR_MODE)
        seams, runner, _out, err = make_seams(self.home, {}, claude=self.claude)
        argv = ["--mode", "install", "--state-dir", str(locked / "state")]
        try:
            code = main(argv, seams=seams)
        finally:
            # Restored here rather than in a cleanup: cleanups run after tearDown has
            # already removed the directory.
            locked.chmod(PRIVATE_DIR_MODE)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("cannot be created", err.getvalue())
        self.assertEqual(runner.calls, [])

    def test_a_state_dir_with_a_control_character_is_refused_by_every_mode(
        self,
    ) -> None:
        for mode in ("install", "tick", "status", "uninstall"):
            for character in (_NUL, _SOH):
                with self.subTest(mode=mode, character=hex(ord(character))):
                    seams, runner, _out, _err = make_seams(
                        self.home, {}, claude=self.claude
                    )
                    target = f"{self.state_dir}{character}x"
                    argv = ["--mode", mode, "--state-dir", target]
                    with (
                        contextlib.redirect_stderr(io.StringIO()) as err,
                        self.assertRaises(SystemExit) as caught,
                    ):
                        main(argv, seams=seams)
                    self.assertEqual(caught.exception.code, EXIT_USAGE)
                    self.assertIn("control character", err.getvalue())
                    self.assertEqual(runner.calls, [])
                    self.assertFalse((self.state_dir / CONFIG_FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
