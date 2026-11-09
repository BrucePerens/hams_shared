# ADR 0117: A Known-Red Baseline for Lint, and a Merge Queue

## Status
Accepted (Bruce, 2026-11-08: "improve how code reaches main so a red main or a queue of ready branches does not stall everyone").

## Scope
The lint run (`tools/run_linters.py`), its pass stamp, the coalescing batches of ADR 0115, the merge helper
(`prmerge.sh`) and a new merge queue worker. Part 1 (the baseline) is in `tools/lint_baseline.py`; part 2 (the queue) is in
`tools/merge_queue.py` once it lands. Operating manual: `docs/runbooks/LINT_MERGE_QUEUE.md`; anchor reference:
`docs/runbooks/LINT_MERGE_QUEUE_ANCHORS.md`.

## Context
When the main branch has a failing lint step or test, every branch's lint run fails too. ADR 0115's batches cannot
attribute such a failure to any branch, split the batch, and end with "failed alone with no attributable file or module
(red main?)": nobody can merge until someone repairs main, and the only way round is the documented skip switch. Separately,
agents spend their time waiting for a lint slot and then a merge, one branch at a time, and have to stay alive until the merge.

## Decision

### Part 1: baseline
1. **A canary records what is red on main.** `run_linters.py --baseline-refresh` lints a clean `origin/main` (a temporary
   worktree, no branch merged, the full run, no cache, the baseline switched off for that run) and records every failing step
   and failing test with the main commit. It does nothing when the record already belongs to the current `origin/main`
   (`--baseline-force` repeats it), runs one at a time per repository, and keeps the old record when the run does not finish.
   It runs after merges (the queue worker does it; `prmerge.sh` does with `PRMERGE_CANARY=1`) and on demand.
2. **What is recorded.** One item per failing test (pytest, unittest and cargo output are read; the shard of a sharded step is
   not part of the id) or, for a step that names no test, one item for the step. Each item has a signature: a hash of the
   normalized failure message (paths, addresses, hex ids and durations removed; other digits kept), or for a step without
   tests a hash of its normalized output lines. Each item has `since`: the oldest main commit at which it was seen red with
   that signature.
3. **A branch run compares.** An item that is in the record with the same signature, and whose `since` commit is in the
   branch's history, is *known red on main*: it is reported in a loud block, does not block, and the run passes. Any other
   failure blocks: a new item, a recorded test that now fails with a different message ("a branch that makes a baselined
   test fail differently still blocks"), or a recorded item whose `since` the branch does not contain (rebase). One new
   failure blocks the run even if others were tolerated.
4. **Never silent.** The pass stamp records ` known_red=<ids> baseline=<main sha>`, `lint_stamp_gate.py` prints a NOTE
   line, `prmerge.sh` shows it, and the report names each tolerated item. `--baseline-show` prints the record;
   `--no-baseline` (`HAMS_LINT_BASELINE=off`) makes every failure block.
5. **Where it lives: a machine-local cache file, not a file in the repository.** The record is
   `~/.cache/hams-lint/baseline/<repository>.json`, written only by the canary from a real run. A tracked `known_red.json`
   (owner and reason, visible in review) was considered and rejected as the source of tolerance: the pull request that adds
   an entry is itself judged against a main that does not have it, so it hits the very dead end it is meant to remove; a
   branch could edit the file to hide its own failures (the tolerant side would have to read it from `origin/main` only,
   and then the owner's entry waits for a merge); and a hand-written entry goes stale without anyone noticing. The cache
   record cannot be edited by a branch, is exact to a main commit, and is refreshed by the machine. Visibility comes from
   the loud report, the stamp field and `--baseline-show`. Owners and reasons belong in the to-do or pull request that
   repairs main.

### Timeouts are not failures (added 2026-11-08)
A step killed by its own time limit while the machine is saturated says nothing about the branch (found the same night: `check_pip_audit` at 300 s and
a relay test, with the load near 90 and PyPI answering in under half a second). Such a failed step (`lint_exec.timeout_suspect`) is rerun once, alone,
after the run; a pass is recorded in the stamp as `timeout-retried=<step>` and printed, a second failure is a real failure. Checkers with a time
limit of their own stretch it by the load (`HAMS_LINT_TIMEOUT_SCALE`, at most 4x). Nothing is hidden: the retry is named in the stamp and the merge output.

### Part 2: merge queue
6. **Enqueue and leave.** `merge_queue.py enqueue <PR>` records a pushed pull request in a queue directory
   (`~/.cache/hams-merge-queue/`) and returns. The agent need not stay.
7. **A one-shot worker** (`merge_queue.py run`, called by a systemd user timer) takes the ready entries in order, builds a
   temporary integration worktree with them merged (`batch_queue.build_combined_tree`), runs
   `run_linters.py --changed --remote auto` on the combined tree (the overflow routing of ADR 0115 amendment, 2026-11-08: this
   machine first, other machines only when it is busy), and merges those that pass through `prmerge.sh`, so its rules
   (pass stamp, markdown exemption, CI-failure refusal, `--admin` squash) apply unchanged. The worker writes the pass stamps of
   the members from its combined run itself, and never pushes to main by any other path.
8. **A failing batch is bisected.** Halves are linted again until the culprit pull request is known; the others merge. A
   merge conflict with the integration tree drops that entry with a comment. Known-red items (part 1) do not fail a run.
9. **The owner is told.** The failure log is written to `~/.cache/hams-merge-queue/results/<PR>.log` and posted as a pull
   request comment; the entry's state (`queued`, `merged`, `failed`, `conflict`) is in `merge_queue.py status`.
10. **Opt-in.** The queue is not enabled: `prmerge.sh` keeps working unchanged. The unit files are in the runbook and no timer is
    enabled by any change. Remote machines are used only through the existing `--remote auto` overflow routing.

## Consequences
A red main no longer stops unrelated branches, but it stays visible on every run. A tolerated failure is a decision made by
the machine from a real run, so the canary must be current: a stale record tolerates less (items fixed on main drop out at the
next refresh), never more. Cost of the canary: one full lint run per main commit that is actually checked (it skips commits
already recorded and collapses a burst of merges into one run of the newest). Cost of the queue: a delay of one timer
interval between enqueue and merge.

## Amendment 2026-11-08: a known-red item is tolerated for a limited time
Bruce: "Let's not let lint issues stand even though we let things pass the known red ones." The baseline must not become a place
where failures live. Each item records `first_seen_time` and an `owner`; every report, the pass stamp (`known_red=<id>(<age>d;<owner>)`)
and `--baseline-show` (oldest first) name the age and the owner. An item older than `HAMS_LINT_BASELINE_MAX_AGE_DAYS` (default 3) stops
being tolerated and blocks, until it is fixed or renewed with a stated reason (`run_linters.py --baseline-renew ITEM --baseline-reason TEXT`).
The test runner's baseline (ADR 0118) has the same rule.
