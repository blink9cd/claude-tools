# Changelog

Notable changes to this project, newest first.

## 0.2.0 - 2026-11-02

### Added

- `os_schedule_gen` replaces `schedule_artifacts`, the library the usage window primer's
  Windows backend uses to build its Task Scheduler task. It still renders one weekly or
  daily schedule as Task Scheduler XML, a systemd service and timer, a launchd plist, and a
  crontab line, and it now has a command line that writes those files into a directory
  without installing any of them, and its `--help` prints no default beside `--force`.
  Its `samples/` folder holds a run to check by hand.
- `help_formatter` gains `RawNoneHidingFormatter` and `UnsetHidingFormatter`, which leave
  out the `(default: ...)` note for an option whose default only means it was not given.
- `stdio_utf8` gains `spell_unsafe`, which writes each character a terminal would act on
  as a visible escape.

### Changed

- `os_schedule_gen`'s default task name, systemd unit name, and launchd label are now
  `\Maintenance\WeeklySweep`, `weekly-sweep`, and `local.weekly-sweep`. The old defaults
  named a tool that is not part of this repository. The usage window primer passes its
  own names, so its task is unchanged.
- `usage_window_primer --help` no longer prints `(default: None)` for `--start`,
  `--last-run`, and `--ping-output-file`.
- The usage window primer's Windows backend refuses a task name that is not a Task
  Scheduler path when it is created, before any `schtasks` command runs. Its own task,
  `\UsageWindowPrimer\Tick`, is unchanged.

### Fixed

- `pip install '.[test]'` works. `pyproject.toml` now declares a build backend and an
  empty package list; before, setuptools refused the layout of side-by-side folders.
- `bounded_run` no longer waits without a bound for the process listing it takes on
  POSIX. Under heavy load on macOS, `ps` could stall for minutes and hold up the tree
  kill; an overdue `ps` is now killed and, after one second, abandoned.
- `os_schedule_gen`'s `build_launchd_plist` and `launchd_plist_path` raise `ValueError`
  for a label outside the set a systemd unit name is held to (letters, digits, `.`, `_`,
  and `-`, starting with a letter or digit). In `schedule_artifacts`, a label such as
  `../../etc/x` produced a plist path outside `~/Library/LaunchAgents`.
- `os_schedule_gen`'s `build_task_xml` raises `ValueError` for a `task_name` that is not a
  task path: a leading `\`, then `\`-separated segments, each in that same set. In
  `schedule_artifacts`, a `..` segment, an empty segment, or markup such as `<Evil/>` was
  accepted.

### Documentation

- The README installs into a virtual environment, runs every command from inside the
  clone, and creates the remote session reviver's task folder before the desktop app's
  folder picker needs it.
- The guides, comments, and test descriptions are reworded for a reader who has only this
  repository.

## 0.1.0 - 2026-10-23

- Initial release.
