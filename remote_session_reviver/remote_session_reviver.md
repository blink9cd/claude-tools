# remote_session_reviver

A Claude desktop app scheduled task that keeps the app's Code-tab sessions reachable from a
phone through Remote Control. `SKILL.md` beside this file is the task's prompt and its source of
truth; the copy the app runs is installed from it (*Sync* below).

## Why it exists

When the desktop app relaunches, to install an update or after a restart, every Code-tab
session comes back stopped with Remote Control off. The phone then cannot reach any of them,
and a stopped session cannot be started from the phone. A message sent to a session from the
Mac starts it again, and the app turns Remote Control back on as it starts. Each run of this
task sends that message to every session whose Remote Control is off and that has been idle
for at least an hour. A session active more recently is probably still running, and a message
re-enables Remote Control only by starting a stopped session, so pinging it would only drop an
unrelated turn into a conversation in use.

A session the previous run pinged (or flagged) that is still off is flagged instead of pinged
again: the run's report lists it under "Needs its Remote Control switch". The run reads the
previous run's report to find these. A cold start restores Remote Control only for a session the
app remembers as on, so a session that never had it, or had it off before it stopped, stays off
however often it is pinged (seen 2026-10-21: no `rcAutoEnable` verdict for it at all). Turning
its switch on once is enough; the app remembers it from then on.

What the app's own log showed, and what it rules out:

- An update relaunch logs `[updater] Update downloaded` and, some minutes later,
  `[event-loop-stall] update relaunch`; every session drops at that moment, whatever its idle
  time.
- A session that a message cold-starts logs `[rcAutoEnable] verdict: enable=true ...
  trigger=cold_resume`, and its Remote Control is back on by the next run.
- Code sessions log `[WarmLifecycle:session] Idle timeout disabled`: the app has no idle
  shutdown for them, so the drop is not an idle timeout.

The app's log is `~/Library/Logs/Claude/main*.log` on macOS.

## Desktop only

The problem this task solves belongs to the desktop app, because the app hosts every Code-tab
session itself: one relaunch stops them all, and none starts again until something sends it a
message. A `claude` session run in a terminal is its own process, and as far as we know (not
tested here) a CLI update applies at that process's next launch rather than stopping the
running one, so terminal sessions should not need this task.

No app setting replaces it. **Connect new sessions to Remote Control** (the Code tab's settings)
only acts when a session starts. It is what turns Remote Control back on at a cold start, but it
cannot start a stopped session. The app also keeps only a few sessions live at once (it logs
`CliGovernor at cap=6`, evicting the idlest), so after a ping some sessions are still running
and others are stopped again.

## What a run cannot do

- **Turn Remote Control on itself.** The app refuses `set_remote_control` to a run nobody is
  watching. A session that starts without Remote Control coming back, or one that was still
  running when pinged (no cold start, so no auto-enable), stays off. The run's report lists it;
  turn it on from its Remote Control switch, or from a session you are watching, which must
  first send it a message, because the switch also refuses a stopped session ("Remote Control
  requires an active session. Send a message first.").
- **Run while the app is closed or the Mac is asleep.** Desktop scheduled tasks fire only while
  the app is open and the computer is awake.
- **Rely on a documented contract.** It uses the app's internal session-management tools
  (`list_sessions`, `send_message`, `archive_session`, `list_events`, `get_usage`, and the scheduled-tasks
  server's `list_task_runs`). The public desktop documentation says Claude "can't send
  cross-session messages from a session nobody is watching", naming a scheduled-task run as one
  such session. The pings deliver today regardless, so an app update may stop them without notice.

## Install

1. Make the task's folder, so the folder picker in step 2 can select it:

   ```bash
   mkdir -p "$HOME/.claude/scheduled-tasks/remote-session-reviver"
   ```

2. In the desktop app, create a scheduled task named `remote-session-reviver` (the prompt finds its
   own runs by that ID), on the cron schedule
   `7 * * * *` (hourly; the app adds a fixed per-task delay), with these settings from the
   instructions box's toolbar and the row below it:
   - **Permission mode: Auto.** Archiving a session prompts for approval in Manual mode, which
     stalls an unattended run.
   - **Model: Haiku.** The work is mechanical, and each run's result is checked by the next
     run's report, so a larger model buys nothing.
   - **Folder: the task's own folder,** `~/.claude/scheduled-tasks/remote-session-reviver`, not a
     repository: a run reads no files, and a repository's instruction files would be re-read on
     every one of its model calls. The folder picker hides `~/.claude`; open that folder in
     Finder (**Go → Go to Folder…**) and drag its title-bar icon onto the picker.
3. Replace its prompt with this folder's `SKILL.md` (*Sync*).

The schedule, permission mode, model, and folder live in the app, not in `SKILL.md`.

Measured on 2026-10-21, a run costs about 410,000 input tokens (almost all cache reads) and
2,700 output tokens over five model calls, whether or not it pings anything: nearly all of it
is the run's own starting context, re-read on each call.

## Sync

The app reads the installed file fresh on every run, so installing a change is one copy, run
from the folder that holds this file:

```bash
cp SKILL.md "$HOME/.claude/scheduled-tasks/remote-session-reviver/SKILL.md"
```

Edit this folder's copy, then copy it over. Never edit the installed copy directly. Saving the
task from the app's Edit form rewrites the installed copy without its final newline, so copy
again after any such save. To check that they match:

```bash
diff SKILL.md "$HOME/.claude/scheduled-tasks/remote-session-reviver/SKILL.md"
```

## Verify

Read the task's latest run, either from its **Scheduled** entry in the app's sidebar or by asking
Claude in any session to `list_task_runs` for `remote-session-reviver` and read that run's events.
The report's table lists each session's `remoteControlActive` and whether it was pinged; a
session pinged in one run should show Remote Control on in the next. Each run also archives the
task's earlier runs, matched by ID from the task's own run list, so no other session is ever
archived.
