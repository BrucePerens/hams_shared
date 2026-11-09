# ADR 0115: Lint and Test Runs Coalesce into Batches

## Status
Accepted (Bruce, 2026-11-04: "the shared lint and test queue on the dev box is our main bottleneck").

## Scope
The shared dev box's lint run (`tools/run_linters.py --changed`, serialised by `lint_lock.py`) and Odoo test run
(`tools/test.py -u <modules>`, serialised by its flock and `lock_queue.py`), and the pass records that
`devbox_tools/prmerge.sh` reads. The mechanism is `tools/batch_queue.py`; the lint side is `tools/lint_batch.py`;
the operating manual and the anchor reference are `docs/runbooks/LINT_TEST_BATCHING.md` and
`docs/runbooks/LINT_TEST_BATCHING_ANCHORS.md`.

## Context
Ten agents each ask for a run of their own branch, one at a time. Every request paid a whole run (5 to 40
minutes at load 20) although most of the work (the tool steps, anchors, the repository-wide checkers) is the
same for all of them and the changes rarely interact. The queue was fair but its throughput was one branch per
run.

## Decision
1. **Coalesce.** (Amended 2026-11-08, ADR 0116: local test batches may run as many at once as there are test slots; the rule below is the default of one.)
   Per machine, per repository and per kind (lint, test) there is at most one running batch and
   one forming batch. With nothing running a request runs at once. A request that arrives while a batch runs
   joins the forming batch with its branch tip and scope (lint: the steps it needs; tests: its modules). When
   the running batch ends, the oldest live member of the forming batch seals it and runs it.
2. **One run over a combined tree.** The leader creates a temporary git worktree under `~/workspace/tmp` at the
   base (`origin/main`), merges each branch tip in (a branch that conflicts is dropped and told to run alone
   as before), runs the linter or tests once and removes the tree.
3. **Every caller still gets its own answer.** A caller blocks until its own verdict is known and exits with its
   own code, prints its verdict and the log path; `prmerge.sh` and agents' commands are unchanged. A pass writes
   the pass stamp of every included tip (the same file `prmerge.sh` checks, same format plus `batch=N`).
4. **Failures are attributed, passes are not held back.** A failing step is attributed to the branches whose
   changed files (lint) or modules (tests) it names. Branches whose files and modules checked clean are marked
   passed at once (Bruce's chosen default). A failure that names no branch's file or module (a red main, a
   repository-wide check) splits the batch in halves and reruns, bounded to nine runs, so a passing branch is
   not held back by a failing one.
5. **Caps.** A batch holds at most 8 branches (`HAMS_BATCH_MAX_BRANCHES`) and, where durations are recorded, an
   estimated 30 minutes of work (`HAMS_BATCH_MAX_SECONDS`); beyond that a request waits for the next batch.
6. **What stays solo.** Uncommitted trees, `--only`/`--skip`/module names, `--solo`, the cheap bypass,
   `HAMS_LINT_NO_LOCK`, runs started by other runs and hams_shared checkouts run alone as before. Repositories
   are never mixed: the queue key is the repository's main checkout.
7. **Robust.** Waiters and the runner are recorded with pid and process start time; a dead waiter is dropped
   when the batch seals, a dead runner frees the running slot and its members re-queue themselves (at most three
   times, then they run alone); the verdict file is written before the running slot is released.
8. **Remote machines.** The leader's combined run goes through `run_linters.py` like any run. One running batch per
   dev box and repository is the limit; per-target-machine batches are a follow-up.
   *Amended 2026-11-08:* the dev box is now the primary lint runner and runs several lint runs at once in slots (4 there); a run
   never goes to another machine because this one is busy (*amended again 2026-11-08*: remote machines only for work this machine cannot run, or by explicit `--remote HOST`). The limit for lint is therefore the slot count (`lint_batch.batches_that_may_run`, as the test
   queue's is the test slot count), not one, and "per target machine" is not a separate claim: the leader's run takes a slot of the machine it
   runs on.

9. **Remote test hosts (2026-11-04).** Odoo tests for hams_com / hams_open branches run on bruce-yoga and the mac1 VM from real git
   clones with their own `origin/main` (deploy keys), never from copied sources. The batch is formed in a queue on the dev box (kind
   `rtest`); the leader splits the modules over all the hosts (a busy host's share waits for it; 2026-11-05) and each host builds the combined tree itself (its `origin/main` plus every
   member's commit, pushed over ssh when GitHub does not have it). Verdict, attribution and pass records are as for local batches.

10. **Splitting divides tests, not installation; environmental failures are not a branch's (2026-11-04).** Found by the first real use (PR #2981): a split put ham_communications_consent on one host and ham_events on another, and ham_events' tests errored for want of the module. Every host installs the whole union and tests only its share. A failing test that also fails on the host's origin/main is environmental: reported, not charged.

## Consequences
Throughput scales with the number of waiting branches instead of staying at one per run. A branch's stamp now
vouches for a run over "main plus the other branches in the batch", not for the branch alone; a failure that
only appears in the combination is reported against the branch whose file it names, and `--solo` repeats a run
alone. Merge conflicts between concurrent branches are discovered earlier. Cost: the leader's temporary
worktree and the extra merge step (seconds).
