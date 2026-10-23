# usage_window_primer.py

Keeps a Claude subscription usage window running while this machine is unattended. A
5-hour window opens on the first message after the previous window ended; this tool sends
a tiny headless message whenever the recorded window has ended, so the next window is
already open when you return.

## How it runs automatically

`--mode install` writes a launchd **user agent** into `~/Library/LaunchAgents` and loads
it. From then on macOS runs `--mode tick` by itself — no terminal, no app, no login
item:

- on every poll slot (minute `0, 5, 10, …` by default);
- once when the Mac **wakes**, if slots were missed while asleep — launchd coalesces
  missed calendar firings into one;
- once at **login**, after a power-off (`RunAtLoad`).

A tick that finds the window still open exits in milliseconds without contacting
anything. Nothing runs while the Mac is asleep or powered off; the catch-up ping goes out
on wake or login instead.

Install waits for any previous copy of the agent to unload before loading the new one,
then confirms the load by reading it back from launchd. Uninstall removes the plist only
after launchd confirms the agent is gone, so a failed read-back leaves the plist in place
for a retry; the state directory is always kept.

Rejected for the tick, and why:

- **A fixed grid of ping times:** 5 does not divide 24, so no grid is stable across days,
  and a catch-up ping opens a window that the next grid ping lands inside, leaving a gap
  before the following one. Reset times drift after an outage instead, by choice.
- **Skipping missed grid slots after an outage:** leaves no window running for up to 5 hours.
- **Inferring the window from local transcripts, or a tool such as `ccusage`:** blind to
  usage from other devices, and a second-hand proxy for a value the ping returns directly.
- **A desktop-app scheduled task:** each run is a full agent session, far heavier than one
  headless call, and it runs only while the app is open.

## Usage

Install once, from a shell where `claude` is on `PATH`, from a copy of this project that
will stay where it is. Install records three absolute paths: the Python interpreter that
ran it (Python 3.12 or newer is required), this script, and `claude`. If any of them moves
— a checkout or worktree that is later removed, an interpreter replaced by an upgrade —
re-run install. Every command here runs from this project's root directory, the folder
holding `usage_window_primer/`, and `python` means that interpreter, Python 3.12 or newer.

```bash
python usage_window_primer/usage_window_primer.py --mode install
```

Start no earlier than a given moment, and record a ping you already sent by hand:

```bash
python usage_window_primer/usage_window_primer.py --mode install --start 2026-09-14T06:00 --last-run 2026-09-13T17:20
```

`--last-run` must not lie in the future (exit 2): a ping cannot have happened yet, and
its window would hold every tick back until five hours after that moment.

Install records `config.json` (and `state.json`, when `--last-run` is given) **before** it
loads the agent, because `RunAtLoad` fires a tick the moment the agent loads and that tick
reads those files. So a refused install (exit 1) leaves behind the new `config.json`, the
new plist — which launchd loads at the next login — and, without `--last-run`, the
existing `state.json`. Re-running `--mode install` is the remedy; `--mode uninstall`
removes the plist.

Check it, and stop it:

```bash
python usage_window_primer/usage_window_primer.py --mode status
python usage_window_primer/usage_window_primer.py --mode uninstall
```

`--mode status` prints the configured start; a `last attempt` row with when the last ping
was sent and its outcome, whatever that outcome was; a `last success` row with when a ping
last got through (`ok` or `limited`, or the `--last-run` moment install recorded); the
recorded window end and its source; when the next ping is due; the consecutive-failure
count; a `scheduler (launchd agent)` row reading `loaded`, `not loaded`, or
`unknown (<error>)` when launchd cannot be read; and last a `health` row, `ok`, or
`failing (<n> pings in a row)` during a failure streak (see *When it keeps failing*). It
exits 1 while that streak lasts and 0 otherwise, whatever the agent's state. A
`state.json` written before attempt times were recorded, by a tick whose ping failed,
shows `last attempt unknown (failed)` until the next ping.

## The state directory

Every tick runs the `claude` that `config.json` names, from the state directory, so an
account that can write there can choose what runs as you. Off Windows, install and
every tick therefore refuse, with exit 2 and the reason, a state directory that is
not owned by the user running them or that its group or anyone else can write. Install
creates a missing one owner-only (`0700`). It never changes the mode of one that already
exists: a directory only its owner can write passes as it is, at `0755` as readily as at
`0700`. Fix a refused one with `chmod go-w` on it, or point `--state-dir` at a directory
of your own. Windows skips the check: its permissions are ACLs, which this check does not
read. The host that runs the tool decides which applies, since the check reads that
host's own filesystem.

A `--state-dir` holding a control character or an undecodable byte is refused in every
mode (exit 2), before anything is written: no path operation, plist or task file could
carry it.

## After installing, confirm it works

A minute after install, run `--mode status`. Expect `scheduler (launchd agent)` to read
`loaded` and `health` to read `ok`. If that first tick pinged, expect `last attempt … (ok)`,
`last success` at the same moment, and `window ends … (server)`. If you installed with a
future `--start`, or with a `--last-run` whose window is still open, the first tick waits
instead: expect `last attempt never (none)` and `last success never`, or `last success`
at the `--last-run` time with `window ends … (manual)`, until the next ping is due.

If the last attempt reads `(failed)`, `last success` shows when a ping last got through.
Read `usage_window_primer.log` in the state directory, then `launchd.err.log` beside it. A tick
that never ran at all — for example because the recorded interpreter moved — writes to
neither, and appears only in the macOS system log.
A `claude` installed through npm is a Node script, so it also needs `node` reachable from
launchd's minimal `PATH`.

## When it keeps failing

A failed ping is retried on the next tick, and a short run of failures shows nothing.
When 12 pings in a row have failed — an hour of ticks at the default 5-minute poll while
the Mac is awake — the tick shows a macOS notification titled
`Claude usage window primer is failing`, with the count and the latest failure's detail. It shows
once per streak, not once per tick. The first ping to get through after that shows a
second one, `Claude usage window primer recovered`, naming how many failures it ended.

The notifications are sent through `osascript`, so System Settings > Notifications lists
them under **Script Editor**: allow them there, and choose their style. If `osascript`
cannot run or reports an error, the tick writes a
`WARNING: could not show the alert notification: …` line to `launchd.err.log` in the
state directory; its exit code, `state.json` and its `usage_window_primer.log` line are the same as
if the notification had shown.

While a streak lasts, `--mode status` prints `health failing (<n> pings in a row)` and
exits 1, whether or not anyone saw the notification. `usage_window_primer.log` says why each ping
failed. Fix the cause — for an expired login, sign in to `claude` again from a terminal —
and the next ping that gets through ends the streak.

## On Windows

On Windows, `--mode install` registers a Task Scheduler task instead of a launchd agent.
Every command above works the same way; this section covers what differs. Until the live
smoke test below has run on a Windows machine, this backend is proven only against a
scripted `schtasks`.

**Prerequisites.** Python 3.12 or newer from python.org, which ships `pythonw.exe` beside
`python.exe`. The Microsoft Store's Python is not supported. The Claude CLI must be on
`PATH` as `claude.exe`, and signed in. Install refuses with exit 1 when there is no
`pythonw.exe` beside the interpreter that ran it, or when `USERDOMAIN` or `USERNAME` is
unset, so it cannot name the logon trigger's user (in both cases after `config.json` is
written). It refuses with exit 2 when `claude` resolves to a 0-byte Microsoft Store app
alias under a `WindowsApps` folder, which a scheduled task cannot launch, or to a
`claude.cmd` or `claude.bat` shim. A shim runs through `cmd.exe`, which parses the
command line by its own rules, so the quoting that protects the prompt and the other
arguments for a real executable does not protect them there. A tick refuses a shim too
(exit 2, without pinging), so a `config.json` recorded before install refused them stops
running one; re-run install once the real `claude.exe` is first on `PATH`.

**Install** from PowerShell 5.1, in this project's root directory, as on macOS. No
elevation is needed:

```powershell
python usage_window_primer\usage_window_primer.py --mode install
```

**What it registers.** One task, `\UsageWindowPrimer\Tick`, whose action runs
`pythonw.exe usage_window_primer.py --mode tick` against the state directory. Because the
interpreter is `pythonw.exe`, a tick opens no console window, and every child it starts
runs with `CREATE_NO_WINDOW`, so none of them does either. Install, status and uninstall
run `schtasks` with the same flag, as `%SYSTEMROOT%\System32\schtasks.exe`, never by a
`PATH` lookup (`C:\Windows` stands in when `SYSTEMROOT` is unset). The task has two
triggers:

- a time trigger from the minute of install, repeating every `--poll-minutes`
  indefinitely;
- a logon trigger for the user who installed it, the twin of launchd's `RunAtLoad`.

It runs as `InteractiveToken` at least privilege, so it runs only while that user is
signed in. Its settings include `IgnoreNew`, so a tick still running when the next one is
due makes the new one skip, and `StartWhenAvailable`, so a slot missed while the machine
slept runs once it wakes. Install confirms the registration by reading the task back with
`schtasks /Query`.

**The files.** The state directory defaults to `%LOCALAPPDATA%\usage_window_primer\`. It
holds `config.json`, `state.json` and `usage_window_primer.log` as on macOS, plus:

- `task.out.log` and `task.err.log`, where a tick's own output lands, because
  `pythonw.exe` gives it no console streams;
- `task.xml`, the UTF-16 task definition install handed to `schtasks /Create`, kept as the
  record of what was registered.

As on macOS, install writes `config.json` before it registers the task, so a refused
install leaves the new `config.json` behind; re-running install is the remedy.

**Status and uninstall.** `--mode status` prints a `scheduler (Task Scheduler task)` row
reading `registered (<status>)`, where the status is Task Scheduler's own (`Ready`,
`Running`, `Disabled`, …), `not registered`, or `unknown (<error>)`. Its exit code
follows the same rule as on macOS. `--mode uninstall` deletes the task and confirms it is
gone; it keeps `task.xml` and the rest of the state directory.

**Failure-streak alerts are not shown on Windows yet.** A tick that would have shown one
writes a `WARNING: could not show the alert notification: …` line to `task.err.log`
instead. The streak itself still counts, and `--mode status` still prints
`health failing (<n> pings in a row)` and exits 1 while it lasts, so check `status` when
the machine has been unattended.

**What leaves no trace.** Anything a tick prints before its log streams open, an
argparse error included, is lost under `pythonw.exe`. The scheduled command line is
generated by install, so an argument error there means the install itself is stale:
re-run install. A tick that never started at all shows only in Task Scheduler's
**Last Run Result** for the task.

**The live smoke test** registers a probe task under its own `\UsageWindowPrimerProbe\`
folder, never the real one, reads it back, removes it and reads it back as gone. Its time
trigger starts a day ahead, so the probe never runs a tick. Run it from PowerShell 5.1:

```powershell
$env:USAGE_WINDOW_PRIMER_LIVE_TASK = "1"
python usage_window_primer\tests\test_task_scheduler_live.py -v
Remove-Item Env:USAGE_WINDOW_PRIMER_LIVE_TASK
```

## Options

| Option | Default | Modes | Meaning |
| --- | --- | --- | --- |
| `--mode` | required | — | `install`, `uninstall`, `tick` or `status` |
| `--state-dir` | `$HOME/.usage_window_primer`; on Windows `%LOCALAPPDATA%\usage_window_primer` when `LOCALAPPDATA` is set | all | Holds `config.json`, `state.json`, `usage_window_primer.log` |
| `--start` | none | install | No ping before this ISO 8601 moment; without an offset it is local time |
| `--last-run` | none | install | A moment a ping is known to have happened, not in the future; the window end becomes it plus 5 hours, source `manual`, until a real ping reports the true end |
| `--model` | `haiku` | install | Model the ping uses; must not hold a control character |
| `--prompt` | `Reply with the single word: ok` | install | Text the ping sends; must not be blank, start with `-`, hold a control character (a tab included), or be fewer than two words, since one word could name a `claude` subcommand |
| `--poll-minutes` | 5 | install | Minutes between ticks; must divide 60 |
| `--ping-output-file` | none | tick | Classify a recorded ping output instead of running `claude` (see `samples/`); refused (exit 2) without an explicit `--state-dir`, so it never writes the installed record |

Re-running `--mode install` with different options replaces the recorded settings and
reloads the agent.

## The ping

```bash
CLAUDE_CODE_DISABLE_CLAUDE_MDS=1 claude -p "$PROMPT" --model "$MODEL" --setting-sources "" --tools "" --strict-mcp-config --no-session-persistence --max-turns 1 --output-format stream-json --verbose
```

It runs from the state directory with a 120-second timeout and an empty stdin, so a run
started from a pipe never waits on it. The environment variable and the empty
`--setting-sources` keep instruction files, hooks and plugins out of the call;
`--tools ""` means it cannot run anything; `--strict-mcp-config`, given no
`--mcp-config`, starts no MCP server, so the user's own servers are not launched for
every ping; `--no-session-persistence` keeps it from writing a transcript. Measured
cost: 3,697 input tokens on 2026-10-19. When the flags were first chosen, on 2026-09-13, a
lean ping cost 6,710 against 31,613 for a default call. `--bare` is not used: it refuses
subscription authentication.

The ping inherits the tick's environment minus the variables `PING_ENV_DROPPED` in
`src/ping_runner.py` names: the authentication ones, which make the CLI use a key or
token instead of the subscription, and the endpoint-steering ones, which send the call
to another endpoint or provider. With any of them set, a ping would bill API credit, or
open no subscription window, while still logging `ok`.

The window end is read from the ping's own `rate_limit_event`, so a window opened from
another device is still timed correctly.

## What a tick records

Every ping attempt records its time and its outcome, whatever the outcome — `status`
prints them as `last attempt`. What else it records depends on the outcome:

| Outcome | Recorded |
| --- | --- |
| `ok` | The ping time as the last success, and the reported window end (source `server`), or ping time plus 5 hours when none was reported or the reported one was refused (source `estimated`) |
| `limited` | The ping time as the last success, and the reported reset (source `server`), so the tick waits instead of retrying; a refused reset is estimated the same way |
| `failed` | The failure count, but not the last success or the window end; the next tick retries — offline, timeout, error exit, an error result, or no result |

A reported window end is believed only when it falls after the ping and at most 5 hours
and 15 minutes later: the window cannot end before the ping that found it running, nor
more than a window's length after it, and the 15 minutes allow for this machine's clock
running behind the server's. Any other end is refused, because one in the past would have
every tick ping again and one far ahead would stop the pings until then. The log line's
detail then names the refused end and the bound it fell outside, and says it was
`estimated instead`.

Every ping attempt appends exactly one line to `usage_window_primer.log`, in the form
`<utc> <kind> window_ends_at=<utc or none> <detail>`, and the log keeps its newest 1,000
lines. Its detail has every non-printable character, a terminal escape among them,
turned into a space, so reading the log in a terminal never runs a sequence the CLI's
text carried. Idle ticks write nothing. launchd's own captures of the tool's output are
`launchd.out.log` and `launchd.err.log` beside it. A run with no console at all, which has
no output streams to capture, appends its output to `task.out.log` and `task.err.log`
there instead.

A failed ping's detail is built from the evidence the ping left:

- **A timeout** reads `exit 124: timed out after 120s`, then `; stderr: <text>` when the
  CLI had written any, then the last stream event it wrote — for example
  `exit 124: timed out after 120s; last event: system/init`. The event (`system/init`,
  `no output`, `no complete event`, ...) shows how far the CLI got before the 120-second
  bound.
- **Any other failing exit whose output held a failed result event** gives the result's
  reason — the CLI's own error text, when the result carries any. Only when stderr is
  not blank does `; stderr: <text>` follow it, so the log's length cap cuts stderr before
  it cuts the error: `exit 1: CLI error: <message>; stderr: <text>`. The headline case,
  an expired login, exits 1 with empty stderr and an error result, and logs
  `exit 1: CLI error: <message>`.
- **Any other failing exit** — no result event, or a result that reported success —
  gives the CLI's stderr alone. A success result's reason never leads the stderr that
  explains the failure. When stderr is empty, the stream's own reason stands in, such as
  `no result event in the ping output`.
- **A clean exit** that still failed gives the stream's own reason: the result's, quoting
  the CLI's error text when it carries any, or `no result event in the ping output`.

## Exit codes

- `0` — success, including a tick that had nothing to do, a `limited` ping, and
  `--mode status` with no failure streak, whatever the agent's state.
- `1` — a ping failed, `--mode status` found a failure streak (see *When it keeps
  failing*), or, for `--mode install` / `--mode uninstall`, the scheduler did not
  confirm the job (on Windows, also an install with no `pythonw.exe` beside the
  interpreter).
- `2` — bad arguments (including an unreadable `--ping-output-file` or one given
  without `--state-dir`, a `--state-dir` holding a control character, a
  `--poll-minutes` that does not divide 60, a `--start` or `--last-run` that is not a
  valid ISO 8601 moment, a `--last-run` in the future, a blank `--prompt`, one starting
  with `-`, one of fewer than two words or one holding a control character, a `--model`
  holding a control character, or a `claude` at install that is not on `PATH`, cannot
  be read, is a 0-byte Microsoft Store app alias, or on Windows is a `.cmd` or `.bat`
  shim), a state directory another user owns or can write (see *The state directory*)
  or that install cannot create, no install yet, a tick on Windows whose `config.json`
  records a `.cmd` or `.bat` shim, or an unreadable `config.json` / `state.json`, a
  control character in one of its text fields included (the run that reports it leaves
  the file untouched).

## Running the tests

```bash
python usage_window_primer/test_usage_window_primer.py
```

No test contacts the network or `claude`, and the only one that reaches launchd is the
launchctl contract test, which prints a label no install ever creates. The tests that
run `osascript` by default give it a script that returns its arguments instead of
showing them, so nothing appears on screen. The tool runs `launchctl`, `id` and
`osascript` by their absolute paths (`/bin/launchctl`, `/usr/bin/id`,
`/usr/bin/osascript`), and so do these tests, so a stand-in first on PATH cannot answer
them. `USAGE_WINDOW_PRIMER_OFFLINE=1` skips them and the launchctl contract test, for a run that
must not reach the real binaries. Every other test runs them only through a scripted
stand-in. The live tests are opt-in: the ping, because it
spends usage, the notification, because someone must watch the screen to confirm it
appeared, and, on Windows only, the Task Scheduler round trip, because it writes to the
real scheduler (its PowerShell commands are under *On Windows*):

```bash
USAGE_WINDOW_PRIMER_LIVE_PING=1 python usage_window_primer/tests/test_live_ping.py -v
USAGE_WINDOW_PRIMER_LIVE_NOTIFY=1 python usage_window_primer/tests/test_live_notify.py -v
```
