# claude-tools

Small, local tools for working with a Claude subscription. Each tool lives in its own folder
with its own guide; the folders beside them that hold no tool are the shared libraries the
tools and their tests use, shipped with their own tests.

| Tool | What it does |
| --- | --- |
| [`usage_window_primer`](usage_window_primer/usage_window_primer.md) | Keeps a 5-hour usage window running while the machine is unattended |
| [`remote_session_reviver`](remote_session_reviver/remote_session_reviver.md) | Brings Claude desktop Code sessions back within reach of your phone after the app relaunches |

Not affiliated with or endorsed by Anthropic.

## usage_window_primer

A Claude subscription's 5-hour usage window starts on the first message after the previous
window ended. If you step away when a window ends, the next one does not start until you
come back. usage_window_primer sends a tiny headless message as soon as the recorded window has
ended, so a fresh window is already running when you return.

### Requirements

- macOS, with the Claude Code CLI (`claude`) on `PATH` and signed in.
- Python 3.12 or newer. The stock `python3` on macOS is too old; install 3.12 or newer,
  for example from python.org or Homebrew. The standard library is enough to run the tool;
  the tests need Hypothesis.
- Windows: a Task Scheduler backend is included but has not been run on a Windows machine
  yet. Treat it as untested.

### Install

```bash
git clone <repository-url> claude-tools
python3.12 claude-tools/usage_window_primer/usage_window_primer.py --mode install
python3.12 claude-tools/usage_window_primer/usage_window_primer.py --mode status
```

Install writes a launchd user agent and loads it, recording the absolute paths of the
interpreter, the script and `claude`; if any of them moves, run install again. From then on
macOS runs the tool every few minutes, on wake and at login, with no terminal or app open.
`--mode uninstall` removes the agent and keeps the recorded state. Every option, the exit
codes and what each run records are in [`usage_window_primer/usage_window_primer.md`](usage_window_primer/usage_window_primer.md).

### The ping and what it costs

```bash
CLAUDE_CODE_DISABLE_CLAUDE_MDS=1 claude -p "Reply with the single word: ok" --model haiku --setting-sources "" --tools "" --strict-mcp-config --no-session-persistence --max-turns 1 --output-format stream-json --verbose
```

It loads no instruction files, hooks, plugins or tools, and writes no transcript. Measured
on 2026-10-19: 3,697 input tokens per ping. A successful ping goes out at most once per window, about five a day if the machine is awake all day; a failed one is
retried on the next tick. A ping counts against your
usage like any other message.

### Where the window end comes from

Each ping's stream output includes a `rate_limit_event` carrying the window's reset time,
and the tool times the next ping from it. That event is observed in the CLI's output, not a
documented interface: a CLI update could change or remove it. When it is missing, the tool
falls back to the ping time plus five hours.

### Known limits

- Nothing runs while the machine is asleep or off; the catch-up ping goes out on wake or
  login.
- A ping can run slowly under launchd; it is bounded at 120 seconds and retried.
- After 12 failed pings in a row it shows a notification. That notification has not yet
  been confirmed live under launchd, and it is macOS-only.

### Use at your own risk

This tool automates messages from your own account. Check that doing so fits your plan's
terms before you install it. It is provided as-is, with no warranty.

### Alternatives

| | usage_window_primer | CCLimitPing | goodmorning-claude, claude-session-refresh | Claude Code routines |
| --- | --- | --- | --- | --- |
| Runs on | your Mac (launchd) | your machine | GitHub Actions | Anthropic's cloud |
| Runs with the machine off | no | no | yes | yes |
| Handles your OAuth token itself | no: `claude` does | yes: reads it from the Keychain | yes: stored as a GitHub secret | no: first-party |
| Where the window end comes from | the ping's `rate_limit_event` (observed, undocumented) | the `api/oauth/usage` endpoint (undocumented) | not surveyed | not surveyed |
| Catches up after an outage | yes | yes | not surveyed | not surveyed |
| Schedule and cost | at each window end, one minimal ping | not surveyed | not surveyed | hourly at most; each run is a full session, under a daily run cap |
| Install | clone, Python 3.12 or newer | `curl \| sh` or a prebuilt binary | a GitHub Actions workflow | built in |

Choose this tool if you want nothing to read or store your OAuth token and everything to
stay on your Mac; choose a cloud option if the machine is often off.

## remote_session_reviver

If your Claude Code sessions stop answering on your phone while you are away, the desktop app
has probably relaunched, to install an update or after a restart: every session comes back
stopped with Remote Control off. remote_session_reviver is a desktop scheduled task that, once an
hour, sends each of those sessions a one-line message from the Mac, which starts it again and
brings its Remote Control back.

### Requirements

- The Claude desktop app on macOS, with Remote Control.
- The Mac awake and the app open: scheduled tasks run only then.

### Install

1. In the app's **Code** tab, open **Scheduled**, create a task named `remote-session-reviver`, and
   give it these settings:
   - Schedule: hourly (`7 * * * *`).
   - Permission mode: **Auto** (Manual mode asks before archiving, and nobody is there to
     answer).
   - Model: **Haiku**.
   - Folder: the task's own folder, `~/.claude/scheduled-tasks/remote-session-reviver`.
2. Copy this folder's `SKILL.md` over the task's prompt:

```bash
cp claude-tools/remote_session_reviver/SKILL.md "$HOME/.claude/scheduled-tasks/remote-session-reviver/SKILL.md"
```

The full steps, including how to reach the hidden `~/.claude` folder in the picker, are in
[`remote_session_reviver/remote_session_reviver.md`](remote_session_reviver/remote_session_reviver.md).

### What a run does and costs

Each run lists your sessions, pings the ones with Remote Control off that have been idle for an
hour or more, flags any that a ping already failed to bring back, archives its own earlier runs,
and ends with a short report. Each run is a small Haiku session, mostly cached input (about
410,000 tokens), run 24 times a day. It uses your plan's usage like any other session, so check
it fits before installing.

### What it cannot do

- Turn Remote Control on for a session that never had it. The app restores Remote Control on a
  restart only for a session it remembers as connected; the report lists the others under
  "Needs its Remote Control switch", and switching one on once is enough.
- Reach a session that is still running; a session used within the last hour is left alone.
- Run while the Mac is asleep or the app is closed.

### Relies on undocumented behavior

This task relies on desktop-app tools that are not publicly documented, and their behavior may
change in any app update.

### Use at your own risk

This task sends messages from your own account into your own sessions. It is provided as-is,
with no warranty.

## Running the tests

With Hypothesis installed (the version the `test` extra in `pyproject.toml` pins), each
suite runs from its own folder's test file:

```bash
python3.12 claude-tools/usage_window_primer/test_usage_window_primer.py
python3.12 claude-tools/bounded_run/test_bounded_run.py
python3.12 claude-tools/help_formatter/test_help_formatter.py
python3.12 claude-tools/live_pid_guard/test_live_pid_guard.py
python3.12 claude-tools/schedule_artifacts/test_schedule_artifacts.py
python3.12 claude-tools/stdio_utf8/test_stdio_utf8.py
```

The live tests stay opt-in; [`usage_window_primer/usage_window_primer.md`](usage_window_primer/usage_window_primer.md) explains
them.

## License

AGPL-3.0-or-later.
