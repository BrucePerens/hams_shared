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
| `HAMS_BATCH_MAX_SECONDS` | 1800 | estimated seconds of work per batch (0 = no cap); lint estimate sums the recorded step durations of the union of steps, unknown steps count 0, divided by the job count -j |
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

* Local TEST batches may run as many at once as there are test slots (`HAMS_BATCH_MAX_RUNNING` / ADR 0116) and so may LINT batches, up to the
  machine's lint slots (`lint_batch.batches_that_may_run`; the dev box 4, a small machine 1): a request starts a batch of its own at once while
  fewer batches than that run, and coalesces with others only while every running slot is busy. The slot is the machine, not a per-target
  claim: the dev box is the primary lint runner and a whole run reaches another machine only as overflow (lint_lock `local_busy`), so there is
  no machine to claim before the batch seals. Remote test batches (`rtest`) keep one running batch, since one batch splits over every idle host.
* The stamp of a batched branch vouches for "main plus the batch" and names the other tips it was checked with
  (`batch=N with=<12 hex>,...`), and a cross-branch interaction is blamed on the branch
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
* Cap: 8 branches, or `HAMS_BATCH_MAX_SECONDS` of estimated work from recorded module durations (`module_durations.json`: the test
  time per module from each host's `odoo.tests.stats` lines; `durations.json`: an equal share of a run's wall time, used when a host
  reported no per-module times; 300 s for an unrecorded module).
* Evidence: `~/.cache/hams-test/passed/<tip>` per passed branch (`tests <time> modules=... batch=N`); nothing gates on it yet.
* Stays solo: `--solo`, `HAMS_TEST_BATCH=off`, `--test-tags`, `-u mod:Class`, any mode but standard, `--mcp`, `--coverage`,
  `--pause-on-fail`, `--offline-isolation`, `--profile`, `--build-daemons`, a dirty tree, a directory that is not the repository root.

## Remote test hosts (bruce-yoga, the mac1 VM)

Odoo tests for a branch run on a remote host through hams_com's `devbox_tools/remote_odoo_test.py <yoga|mac1|auto> <tree> <modules>`.

* **Real clones.** Each host holds git clones of hams_com and hams_open (with the hams_shared submodule) in `/opt/hams-testsrc`, made from GitHub
  with read-only deploy keys (`devbox_tools/provision_test_clones.py <host>`, idempotent, verifies origin/main equals GitHub's main). Nothing is rsynced.
* **Getting a commit there.** The tree under test must be committed. The host fetches the branch from GitHub; a commit that is not on GitHub is
  pushed over ssh to `refs/devbox/<sha12>` in the host's clone. Uncommitted work is refused.
* **The queue.** The caller joins the `rtest` queue of its repository on the dev box (`run_remote_member`). The leader splits the union of the
  batch's modules over all the hosts (`plan_machine_split`, recorded durations; a busy host's share waits for its lock), runs `remote_odoo_test.py <host> ... --members-file`
  once per host in parallel (`run_on_machine`), and each host resets its clones, checks out its own `origin/main`, merges every member tip
  (`--no-ff`) and runs `test.py --solo -d hams_test -l <dir>` as the odoo user. A conflict is reported by the host and sends that branch to run alone.
* **Companions are installed, not tested.** A member may carry `install`: modules its tests need installed (hams_com `asset_gate.py` test companions) that
  it did not ask to test. The hosts install the union of `scope` and `install`; only the `scope` union is split and tested.
* **Verdicts.** As for local tests: failures are attributed to the branches that asked for the implicated modules; a clean combined run passes every
  branch in it; a host that could not start fails the batch with exit 75 ("run on X could not start"). Each host's log is appended to the batch log.
* **Splitting divides the tests, not the installation.** Every host installs/updates the whole union of the batch's modules (`test.py -u <union>`) and tests only its share (`--test-tags /m1,/m2`), because a module's tests may need another requested module to be installed (ham_events' listing tests need ham_communications_consent).
* **Environmental failures.** After a failing run the host re-runs the same modules on its plain origin/main (the baseline). A failing test that also fails there is reported as environmental in the batch log and the passing verdict's note, and is not charged to any branch; the batch does not keep halving over it.
* `--solo` (or `--env`, `--test-tags`, a `:Class` selector) tests the tip alone on one host without the queue (no merge with main).
* Hosts: `HAMS_TEST_MACHINES` (default `yoga,mac1`). One running batch at a time per repository on the dev box; a batch uses all the hosts, waiting for any that is busy.

Not done: a second remote test batch on the hosts the first did not take (a small batch's split leaves hosts unused).
