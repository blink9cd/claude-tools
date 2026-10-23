"""The state directory's guard: a name every layer can carry, and an owner nobody else shares.

Every tick runs the ``claude_path`` that config.json names, from the state directory. So a
directory another account can write to lets that account choose what runs as this user:
on POSIX, install and every tick refuse one that is not owned by the effective user, or
that its group or anyone else can write. A directory install creates is owner-only. An
existing one is never re-moded: one that only its owner can write passes as it is, at
0755 as readily as at 0700. Windows skips the check: it has no POSIX owner or mode bits,
and its ACLs are a model this check does not read. Which kind of host it is comes from the
host that runs the check, never from a platform a caller names: the check stats the real
filesystem, so only that filesystem's kind says whether its mode bits mean anything.

The name is checked too. A NUL in it stops every path operation, and the other control
characters, or a lone surrogate (how an undecodable byte arrives in argv), fail the plist
or task XML write after config.json has already been recorded.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from state_store import has_unsafe_character

# Owner-only: what install gives a state directory it creates.
PRIVATE_DIR_MODE = 0o700
# Write permission for the group and for everyone else.
_FOREIGN_WRITE_BITS = stat.S_IWGRP | stat.S_IWOTH
_PERMISSION_BITS = 0o777


@dataclass(frozen=True)
class DefaultStateDir:
    """The state directory used when ``--state-dir`` was not given.

    It stands as the option's default, so a mode can tell an omitted option from an
    explicit one, while ``--help`` still prints the real directory as that default.
    """

    path: Path

    def __str__(self) -> str:
        """The directory, as ``--help`` prints the default."""
        return str(self.path)


# os.geteuid is POSIX-only. The split is an ``if`` so the win32 type-check pass still
# sees the name; the win32 arm exists only for that pass.
if sys.platform == "win32":

    def effective_uid() -> int:  # pragma: only-on win32
        """No uid: Windows has none, and the guard skips Windows before asking."""
        return -1

else:  # pragma: only-on posix

    def effective_uid() -> int:
        """This process's effective uid, which must own the state directory."""
        return os.geteuid()


def state_dir_text_problem(text: str) -> str | None:
    """Why *text* cannot name a state directory, or None."""
    if has_unsafe_character(text):
        return (
            f"--state-dir {ascii(text)} holds a control character or an undecodable "
            "byte, which no path operation or scheduler file can carry"
        )
    return None


def ensure_private_dir(path: Path) -> str | None:
    """Create *path*, and any missing parents, owner-only; why it could not be, or None.

    An existing directory is left alone. A file already at *path* is left for
    `state_dir_problem` to report. Any other refusal (a parent that is a file or cannot
    be written, a name the filesystem cannot store) is returned rather than raised, so
    install reports it as a state-directory problem instead of a traceback.
    """
    try:
        path.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    except FileExistsError:
        return None
    except OSError as exc:
        # The OSError text already ends with the path, so it is not repeated here.
        return f"the state directory cannot be created: {exc}"
    return None


def state_dir_problem(
    path: Path, *, posix: bool, euid: Callable[[], int]
) -> str | None:
    """Why *path* is unsafe to keep config.json in, or None.

    *posix* says whether the host running this is a POSIX one; callers pass the real
    host's answer (``os.name == "posix"``). None too when *path* does not exist: a tick
    then reports that there is no install, and install creates the directory before it
    asks. On a non-POSIX host nothing is checked.
    """
    if not posix:
        return None
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"the state directory cannot be read: {exc}"
    if not stat.S_ISDIR(info.st_mode):
        return f"the state directory {path} is not a directory"
    if info.st_uid != euid():
        return (
            f"the state directory {path} is not owned by this user (owner uid "
            f"{info.st_uid}, this process {euid()}); every tick runs what its "
            "config.json names, so use a directory of your own"
        )
    mode = info.st_mode & _PERMISSION_BITS
    if mode & _FOREIGN_WRITE_BITS:
        return (
            f"the state directory {path} is writable by other users (mode "
            f"{oct(mode)}); every tick runs what its config.json names, so remove "
            "their write access: chmod go-w"
        )
    return None
