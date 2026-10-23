# usage_window_primer samples

Two hand-verification pairs for `usage_window_primer --mode tick`, each a recorded ping output and
what a tick writes from it: a successful ping and its `state.json`, and a failed ping and
its `usage_window_primer.log` line. `--ping-output-file` stands in for the real ping, so neither
check needs the network or the `claude` CLI, and neither spends usage.

## Regenerate and compare

From this project's root directory, the folder holding `usage_window_primer/`, with Python 3.12 or
newer. Each run copies the
sample state into a fresh scratch directory, so the recipe is safe to run twice:

```bash
dir=$(mktemp -d)
cp -R usage_window_primer/samples/state_dir/. "$dir"
python usage_window_primer/usage_window_primer.py --mode tick --state-dir "$dir" --ping-output-file usage_window_primer/samples/ping_output_ok.jsonl
cat "$dir/state.json"
```

Every field must equal `expected_state.json` except `last_attempt_at` and `last_ping_at`,
which both hold the moment the tick ran, and `window_ends_at`, five hours after it. The
recording reports a window end that has long passed, and a tick believes a reported end
only when it falls within the next five hours (fifteen minutes' slack aside). So the tick
records its own estimate, source `estimated`, and the line it adds to `usage_window_primer.log`
names the end it refused. `usage_window_primer/tests/test_samples.py` makes the same comparison on
every test run, against the real clock as here, so the pair cannot drift from the code.

`--ping-output-file` needs an explicit `--state-dir`: without one, the tick refuses (exit
2) rather than classify the recording into the installed record.

## A failed ping's log line

`ping_output_error.jsonl` is a ping the CLI answered with an error. Its shape was measured
on a real expired login: the result event's `subtype` still reads `success`, only
`is_error` marks the failure, and the CLI's own explanation is in its `result` text. That
wording is illustrative — the real text was not captured. The same recipe, pointed at it,
exits 1 and writes one log line:

```bash
dir=$(mktemp -d)
cp -R usage_window_primer/samples/state_dir/. "$dir"
python usage_window_primer/usage_window_primer.py --mode tick --state-dir "$dir" --ping-output-file usage_window_primer/samples/ping_output_error.jsonl
cat "$dir/usage_window_primer.log"
```

Everything after the line's leading timestamp, which is the moment the tick ran, must
equal `expected_log_error.txt`. `usage_window_primer/tests/test_samples.py` checks this pair on every
test run too.
