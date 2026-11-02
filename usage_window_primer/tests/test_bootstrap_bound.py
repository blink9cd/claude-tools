"""The library bootstraps must never import from a folder planted above the checkout.

usage_window_primer.py and src/process_run.py put stdio_utf8, help_formatter and bounded_run on
sys.path at one fixed place each, the library's folder directly under the repository root
(stdio_utf8/stdio_utf8.md, "How this project's tools import it"). A search of the parent
directories instead could also match a same-named folder above the checkout, such as one
in a shared temporary directory. This copies the tool into a checkout-shaped tree, plants a
decoy of each library above it, and runs the real CLI in a real child process: the only
way to observe which folder an import actually came from.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_TOOL_ROOT / "src"))

from process_run import run_process  # noqa: E402 - import follows the sys.path bootstrap above

import bounded_run  # noqa: E402 - process_run's own bootstrap put it on sys.path
import stdio_utf8  # noqa: E402 - bounded_run's own bootstrap put it on sys.path

_STDIO_UTF8 = Path(stdio_utf8.__file__)
#: usage_window_primer.py's own bootstrap reaches help_formatter, and importing usage_window_primer here would
#: run it; the library sits beside stdio_utf8, directly under the repository root.
_HELP_FORMATTER = _STDIO_UTF8.parent.parent / "help_formatter" / "help_formatter.py"

#: A generous ceiling for the child: a hung CLI must fail this suite, never hang it.
_CHILD_TIMEOUT_SECONDS = 60.0

#: A standard-library module the tool imports only after its bootstraps have run
#: (src/state_store.py), so a decoy of it in an admitted folder would be imported instead.
_SHADOWED_MODULE = "json.py"

#: What a decoy module does when imported: leave a file beside itself to say so.
_DECOY_BODY = (
    'from pathlib import Path\n\nPath(__file__).with_name("imported").touch()\n'
)

#: Appended to each decoy's copy of its library, so importing the library itself from the
#: decoy leaves the same mark. The json.py shadow alone cannot catch that: usage_window_primer.py
#: APPENDS its library folders to sys.path, so a decoy folder admitted there never shadows
#: the standard-library json, and only the library's own import reveals the wrong pick.
_DECOY_LIBRARY_MARK = (
    "\n\nimport pathlib as _decoy_pathlib\n\n"
    '_decoy_pathlib.Path(__file__).with_name("imported").touch()\n'
)


def _plant_library(folder: Path, library: Path) -> Path:
    """Copy the library file *library*, and its private src/ if any, into a new *folder*.

    The src/ travels because a library that owns one imports it at load time from beside its
    own file (bounded_run's drain does), so a copy without it fails to import at all, and a
    decoy that cannot import leaves no mark, which would pass this test for the wrong reason.
    """
    folder.mkdir(parents=True)
    shutil.copy2(library, folder / library.name)
    private = library.parent / "src"
    if private.is_dir():
        shutil.copytree(
            private, folder / "src", ignore=shutil.ignore_patterns("__pycache__")
        )
    return folder


class PlantedAncestorTest(unittest.TestCase):
    """A library folder planted above the checkout is never imported from."""

    def test_a_library_folder_planted_above_the_checkout_is_never_imported(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            above = Path(tmp).resolve()
            checkout = above / "checkout"
            tool = checkout / "usage_window_primer"
            shutil.copytree(
                _TOOL_ROOT,
                tool,
                ignore=shutil.ignore_patterns("tests", "__pycache__", ".*"),
            )
            decoys: list[Path] = []
            for library in (
                _STDIO_UTF8,
                _HELP_FORMATTER,
                Path(bounded_run.__file__),
            ):
                # The real copy where the repository keeps it, and a WORKING decoy above
                # the checkout, so a wrong resolution still runs and leaves its mark.
                _plant_library(checkout / library.parent.name, library)
                decoy = _plant_library(above / library.parent.name, library)
                (decoy / _SHADOWED_MODULE).write_text(
                    _DECOY_BODY, encoding="utf-8", newline="\n"
                )
                copy = decoy / library.name
                copy.write_text(
                    copy.read_text(encoding="utf-8") + _DECOY_LIBRARY_MARK,
                    encoding="utf-8",
                    newline="\n",
                )
                decoys.append(decoy)
            argv = [sys.executable, str(tool / "usage_window_primer.py"), "--help"]
            code, _out, err = run_process(argv, _CHILD_TIMEOUT_SECONDS, None, None)
            imported = [str(decoy) for decoy in decoys if (decoy / "imported").exists()]
        self.assertEqual(imported, [], "a module was imported from a planted folder")
        self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
