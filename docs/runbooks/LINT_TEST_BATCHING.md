# Lint and test batching (coalescing queue)

Decision: `docs/adrs/0115_lint_and_test_runs_coalesce_into_batches.md`. Anchor reference (every function and data
structure, ADR 0113): `docs/runbooks/LINT_TEST_BATCHING_ANCHORS.md`. Code: `tools/batch_queue.py` (queue, combined
tree, attribution), `tools/lint_batch.py` (lint side), the hook in `tools/run_linters.py`.

## What you see

```
$ python3 tools/run_linters.py --changed          # nothing else running
[*] batch 1: nothing is running here; starting at once
...
$ python3 tools/run_linters.py --changed          # while batch 1 runs
[*] joined batch 2 with 2 other branches (batch 1 running for 3 min (1 branch))
[*] batch 2 starts with 3 branches: ai/x, ai/y, ai/z       (printed by the leader)
batch 2 verdicts (log: ~/.cache/hams-lint/batches/lint-<repo>/logs/batch-2.log):
  PASSED   ai/x (1a2b3c4d5e)  <- you
  FAILED   ai/y (...)  2 finding(s) name this branch's files or modules
  PASSED   ai/z (...)
[*] lint batch 2: ai/x PASSED (checked together with 2 other branches; pass stamp written)
```

Your exit code is your own: 0 if your branch passed, 1 if it failed, 75 if `--lock-timeout` ran out. The pass stamp
`~/.cache/hams-lint/passed/<tip>` is written for every branch that passed, so `prmerge.sh` needs no change.
A failing branch prints the findings that name its files and the log path. The log holds the whole combined run.

## How a batch works

1. The caller computes its steps as before. If the run is heavy (it would take the lint lock) and plain (below), it
   joins the queue for its repository (`batch_queue.state_dir("lint", repo_key)`; the key is the main checkout).
2. State is one JSON file under an flock: `forming`, `running`, `next_id`. `join` adds the caller to the forming batch
   (or creates it). The oldest live member of the forming batch is the leader. It seals the batch only when nothing is
   running, so the batch keeps absorbing requests for as long as the previous batch runs.
3. The leader builds `~/workspace/tmp/batch_lint_<repo>_<pid>-<start>_b<id>_r<n>/<repo>`: a detached worktree at
   `origin/main` with each tip merged (`--no-ff`). A conflicting branch is aborted and gets verdict `solo`; its caller
   runs alone exactly as before. `fix_worktree_symlinks.py` links the tree like any agent worktree.
4. The leader runs `nice -n 10 run_linters.py --changed origin/main --solo --results-json R` in the tree. That is an
   ordinary run: it takes the lint lock, waits for the load gate and, with `--remote auto` (the default), may ship the
   whole run to an idle machine. Its output is shown by the leader and kept in the batch log.
5. `settle` turns the result into verdicts. Pass: everyone passes. Fail: each failed step's output is searched for the
   changed files of each branch (`path_finder`); a branch whose file is named fails, all others pass at once. A failure
   that names no branch's file splits the batch in halves and reruns (at most nine runs; both halves failing the same
   way fails everything with a note). The tree is removed.
6. The leader writes the verdict file, then frees the running slot, then writes nothing else: waiters poll the verdict
   file once a second (cheap: one small JSON read).

## What runs alone (never batched)

`--solo`; a dirty tree (only committed tips are batched); `--only`, `--skip` or module names; a cheap or fully cached run
(it takes no lock); `HAMS_LINT_NO_LOCK=1`; runs started by another lint run (`HAMS_LINT_LOCK_HELD`); the combined run
itself; runs started from a hams_shared checkout (its tools are the linter, so a batch cannot test them against another
branch). `HAMS_LINT_BATCH=off` disables batching for a session. The line `[*] lint batch: running alone (<reason>)` says why.

## Knobs

| Variable | Default | Meaning |
|---|---|---|
| `HAMS_BATCH_MAX_BRANCHES` | 8 | branches per batch |
| `HAMS_BATCH_MAX_SECONDS` | 1800 | estimated seconds of work per batch (0 = no cap); lint estimate sums the recorded step durations of the union of steps, 120 s for an unknown step |
| `HAMS_BATCH_DIR` | `~/.cache/hams-lint/batches` | queue state, verdicts, logs |
| `HAMS_BATCH_TREE_DIR` | `~/workspace/tmp` | where combined trees are created |
| `HAMS_LINT_BATCH` | on | `off` = never batch |

## Failure handling

* A waiter that dies is dropped from the forming batch (pid and start time, `lock_queue.is_live`).
* A leader that dies leaves a running slot with a dead runner. The next poll of any member frees it
  ("its runner died; waiting members re-queue"); members join again (at most three times, then they run alone).
  Its combined tree is removed by the next leader (`reap_orphan_trees`).
* The leader receiving SIGTERM or SIGHUP leaves the queue and removes its tree on the way out.
* A stale verdict file (state lost) is ignored: a verdict counts only if it lists the reader as a member.
* The combined run exits 75 (lock or load gate gave up): every member of that batch fails with exit 75.

## Known limits and follow-ups

* One running batch per repository on the dev box, even when its run is shipped to another machine; per-target-machine
  batches would let a second batch run on a second idle machine. To-do filed.
* The stamp of a batched branch vouches for "main plus the batch", and a cross-branch interaction is blamed on the branch
  whose file the finding names. `--solo` repeats a run alone.
* A failure that names no file (anchor and documentation cross-reference checks name anchors, not files) always splits.
* hams_shared tips are not batched.
* The leader runs the whole test union on this machine; splitting it across machines is stubbed (see Tests below).

## Tests (test.py)

`test.py -u ham_a,ham_b` (whole modules, committed tree, repository root) joins the **test** queue of its repository
(`state_dir("test", ...)`), the same state machine with these differences:

* Scope: the modules of `-u`. A batch's run is `test.py -u <union> --solo --wait -l <dir>` in the combined tree; the nested
  test.py takes the box-wide test lock exactly as a single run does (so it also queues behind unbatched runs).
* Attribution: `filtered_test.txt` lists the modules whose tracebacks implicate them; each is blamed on the members whose `-u`
  names it, the other members pass at once. A crash with no implicated module splits the batch. A module broken by ANOTHER
  branch's change (a dependency) is blamed on the branch that asked for that module; `--solo` confirms.
* Cap: 8 branches, or `HAMS_BATCH_MAX_SECONDS` of estimated work from recorded module durations (`durations.json`, an equal share
  of each run's wall time per module; 300 s for an unrecorded module).
* Evidence: `~/.cache/hams-test/passed/<tip>` per passed branch (`tests <time> modules=... batch=N`); nothing gates on it yet.
* Stays solo: `--solo`, `HAMS_TEST_BATCH=off`, `--test-tags`, `-u mod:Class`, any mode but standard, `--mcp`, `--coverage`,
  `--pause-on-fail`, `--offline-isolation`, `--profile`, `--build-daemons`, a dirty tree, a directory that is not the repository root.
* Not done (stubbed): spreading the union over machines. `odoo_test_batch.plan_machine_split` computes the split from the recorded
  durations; `run_on_machine` (to be backed by hams_com `devbox_tools/remote_odoo_test.py`) raises `NotImplementedError`.
  To-do `night_shift_todo/medium/split-test-batches-across-machines-*.md`.
