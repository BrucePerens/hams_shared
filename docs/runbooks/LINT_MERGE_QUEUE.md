# Known-red baseline and merge queue: operating manual

Design and reasons: `docs/adrs/0117_known_red_baseline_and_merge_queue.md`. Anchor reference:
`LINT_MERGE_QUEUE_ANCHORS.md`. This page is how to use it.

## The known-red baseline (on by default)

When main has a failing lint step or test, a branch run prints

    === KNOWN RED ON MAIN (baseline of <sha>): N failure(s) tolerated, not caused by this branch ===
      known red: pytest tools/::tools/test_x.py::Case::test_y   (red on main since <sha>)  <message>

and passes if nothing else failed. A failure that is not in the record, or a recorded test that fails with a different
message, prints `BLOCKS: ...` and the run fails as before.

| Command | What it does |
| --- | --- |
| `run_linters.py --baseline-refresh` | The canary: lints a clean `origin/main` (full run, in a temporary worktree) and records the failing items. Does nothing if the record is already for the current `origin/main`. |
| `run_linters.py --baseline-refresh --baseline-force` | Same, even when current. |
| `run_linters.py --baseline-show` | Prints the record of the repository you are in. |
| `run_linters.py --no-baseline` or `HAMS_LINT_BASELINE=off` | No tolerance: every failure blocks. Use it to see the true state of a branch. |
| `PRMERGE_CANARY=1 prmerge.sh ...` | After a successful merge, refresh the baseline in the background (low priority, one at a time). |

Run `--baseline-refresh` from a checkout or worktree of the repository (hams_com, hams_open, or a worktree of hams_shared;
not from the main hams_shared checkout). The record is `~/.cache/hams-lint/baseline/<repository>.json`.

The pass stamp (`~/.cache/hams-lint/passed/<tip>`) ends with ` known_red=<ids> baseline=<sha>` when it tolerated anything;
`lint_stamp_gate.py` (run by `prmerge.sh`) prints a NOTE line. To repair main, fix the failing test; the next canary run
removes it from the record.

## Timeouts on a saturated machine

A step that fails with a timeout (`timed out`, `did not complete within ...`) while the machine is saturated (CPU pressure at least 25% or
load at least 1.25 x the processors when the step ended) is not charged to the branch at once: after the run it is rerun once, alone. If it passes, the run
passes and the stamp ends with ` timeout-retried=<step>`; `prmerge.sh` prints a NOTE. If it fails again it is a real failure. A step's own time limit
(`check_pip_audit.py`) also stretches with the load (`HAMS_LINT_TIMEOUT_SCALE`, 1 to 4). Switch the retry off with `HAMS_LINT_TIMEOUT_RETRY=off`.
