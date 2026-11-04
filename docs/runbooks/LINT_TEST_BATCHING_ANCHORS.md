# Lint and test batching: anchor reference (ADR 0113)

One entry per anchor in `tools/batch_queue.py`, `tools/lint_batch.py` and `tools/run_linters.py` that belongs to the
coalescing queue. Design and operation: `LINT_TEST_BATCHING.md`; decision: ADR 0115. Tests: `tools/test_batch_queue.py`,
`tools/test_lint_batch.py`.

Contents: [Data structures](#data-structures) - [batch_queue](#batch_queue) - [lint_batch](#lint_batch) - [run_linters](#run_linters) - [odoo_test_batch](#odoo_test_batch)

## Data structures

### `batch_queue:Member`
One waiting request: a plain dict built by `make_member`. Lives in `forming.members` and `running.members` of the state
file. Created by the requesting process, copied into the JSON state, discarded when its batch is finished or the process dies.

| Field | Type | Meaning |
|---|---|---|
| `pid`, `start` | int | process id and `/proc` start time (clock ticks); together the identity checked by `lock_queue.is_live` |
| `branch` | str | name printed in messages (`HEAD` becomes the first 10 characters of the tip) |
| `tip` | str | commit sha of the branch tip; the key of its verdict and of its pass stamp |
| `scope` | list of str | lint: names of the steps it needs; tests: its modules. Used only by the duration estimate |
| `queued_at` | float | epoch seconds; informational |
| `solo` | bool | True: the member needs a batch of its own (`join` refuses to mix it) |
| `info` | str | free text for status output |

Invariants: `(pid, start)` is unique in a batch (`join` ignores a second join); several members may share a `tip` (they share
one verdict). Serialised as JSON; `STATE_VERSION` 1, a different version resets the state. Mutated only inside `locked`.
Functions: `make_member`, `join`, `leave`, `seal`, `prune`.

### `batch_queue:BatchState`
The queue's state file `<state_dir>/state.json` (written atomically by `locked`).

| Field | Type | Meaning |
|---|---|---|
| `version` | int | `STATE_VERSION` (1) |
| `next_id` | int | id the next new batch receives; only grows |
| `forming` | batch or null | accepting members: `{id, members, solo, created}` |
| `running` | batch or null | sealed, being run: `{id, members, solo, runner: {pid, start}, started, log}` |

Invariants: at most one forming and one running batch; `running.runner` is one of `running.members`; ids are unique. Who
may change it: any process, only while holding the flock on `state.lock`. Created by `empty_state`; changed by `join`,
`leave`, `seal`, `finish`, `prune`. Verdicts live beside it in `verdicts/<id>.json` (see `batch_queue:Verdict`), logs in
`logs/batch-<id>.log`. Compatibility: unreadable or other-version files are replaced by an empty state (waiters rejoin).

### `batch_queue:RunResult`
What one run over a group of members returns to `settle`: `{"exit_code": int, "failures": {name: output text},
"conflicted": [(member, text)], "fatal": bool (optional), "detail": str (optional), "selected": [step names] (optional)}`.
`exit_code` 0 = everything passed. `failures` has one entry per failed step (lint) or module (tests). `conflicted` lists
members dropped for a merge conflict. `fatal` means the run could not start (lock timeout, tree creation failed): every live
member fails with `exit_code`, no splitting. Produced by the runner callable (`LintBatch._run_once`), consumed by `settle`.

### `batch_queue:Verdict`
One branch's answer, stored in the verdict file under its tip: `{"status": "passed"|"failed"|"solo", "exit_code": int,
"findings": [str] (at most 40 lines naming the branch), "detail": str, "log": path, "batch": id}`. `solo` means "your merge
conflicted: run alone" (the caller falls back to the normal path). Written by the leader once per batch
(`write_verdicts`), read by every member (`read_verdicts`), kept one day.

## batch_queue

### `batch_queue:repo_key`
Identifies a repository for queueing so that worktrees share a queue and repositories never do. Called by `run_member`.

| Argument | Type | Meaning |
|---|---|---|
| `repo_root` | str | any checkout or worktree of the repository |

Returns the real path of the main checkout (parent of `git rev-parse --git-common-dir`); on any git error the real path
of `repo_root`. No locks; runs one git command. See also `state_dir`.

### `batch_queue:state_dir`
Queue directory for one kind of run in one repository.

| Argument | Type | Meaning |
|---|---|---|
| `kind` | str | `"lint"` or `"test"` |
| `key` | str | `repo_key` result |
| `environ` | dict or None | environment; `HAMS_BATCH_DIR`, else `$XDG_CACHE_HOME`/`~/.cache` + `hams-lint/batches` |

Returns `<base>/<kind>-<key with runs of other characters replaced by _>`. Does not create it (`locked` does).

### `batch_queue:locked`
Context manager: takes an exclusive blocking `flock` on `<sdir>/state.lock`, yields the state dict, saves it on normal
exit (tmp file + rename) and releases. Purpose: every state change is atomic across processes. Special concerns: keep the
block short (no waiting, no running, no printing); an exception inside the block discards the changes; the lock is dropped
by the kernel if the process dies. Creates `sdir`. A missing, unreadable or other-version state file yields `empty_state()`.

### `batch_queue:prune`
Removes dead waiters from the forming batch (an emptied batch disappears) and frees the running slot when its runner is
dead. `prune(state, live=lock_queue.is_live)` returns a list of events (`("runner_died", batch_id)`); mutates `state`; call
inside `locked` at every poll. `live(pid, start)` must be cheap (two `/proc` reads). Idempotent.

### `batch_queue:caps_from_env`
Reads `HAMS_BATCH_MAX_BRANCHES` (int, at least 1, default 8) and `HAMS_BATCH_MAX_SECONDS` (finite float, at least 0, 0 = no
duration cap, default 1800) from `environ`; bad values fall back to the defaults. Returns `{"max_branches", "max_seconds"}`.

### `batch_queue:join`
Adds a member to the forming batch, creating it when none exists.

| Argument | Type | Meaning |
|---|---|---|
| `state` | dict | the state, from `locked` (mutated) |
| `member` | Member | the request |
| `caps` | dict | `caps_from_env` result |
| `now` | callable | clock for `created` |
| `estimate` | callable or None | `estimate(sorted union of scopes) -> seconds or None` |

Returns the batch id, or None when the member must wait: the forming batch is full (distinct tips at `max_branches`, or the
estimate with this member would exceed `max_seconds`; the first member is always accepted) or `solo` rules forbid mixing.
Joining twice from the same process returns the same id without duplicating; a second caller with the same tip shares the
slot. Forming batches accept members while a batch runs: that is the point.

### `batch_queue:leave`
Removes the member (same pid and start) from the forming batch; an emptied batch disappears. Used on timeout and on SIGTERM.
No effect once the batch is sealed.

### `batch_queue:seal`
`seal(state, member, now, log)`: when nothing is running and `member` is the oldest member of the forming batch (call
`prune` first), moves the batch to `running` with `member` as runner and returns it; otherwise None. `log` is the batch log
path stored for status output. The sealed member list is final: later requests form the next batch.

### `batch_queue:finish`
`finish(state, batch_id)` frees the running slot if it still holds that batch. Call after the verdict file is written.

### `batch_queue:write_verdicts`
`write_verdicts(sdir, batch_id, doc, now)` publishes `{verdicts, log, members, batch, finished}` as `verdicts/<id>.json`
atomically and deletes verdict files older than a day. The leader calls it before `finish`, so a waiter never sees a gap.

### `batch_queue:read_verdicts`
`read_verdicts(sdir, batch_id)` returns the verdict document or None when absent or unreadable. Cheap; polled once a second.

### `batch_queue:coalesce`
The caller's whole interaction with the queue: join, wait, possibly lead, return the member's own result.

| Argument | Type | Meaning |
|---|---|---|
| `sdir` | str | queue directory |
| `member` | Member | the request |
| `run_batch` | callable | `run_batch(batch_id, members, log_path) -> {"verdicts": {tip: Verdict}, "log": path}`; called only in the leader process; may block for minutes; if it raises, no verdict is written, the slot is freed and the exception propagates |
| `caps`, `estimate` | | as `join` |
| `poll`, `report_every` | float seconds | poll interval (1 s) and waiting message interval (60 s) |
| `timeout` | float or None | give up waiting after this long (a leader that already started is not interrupted) |
| `out`, `now`, `sleep`, `live` | callables | printing, clock, sleep, liveness (injectable for tests) |
| `handle_signals` | bool | convert SIGTERM/SIGHUP into leaving the queue and exit 128+signal (main thread only) |
| `log_for` | callable or None | `log_for(batch_id) -> log path` |

Returns `{"status": "passed"|"failed"|"solo"|"timeout"|"gave_up", "verdict", "batch", "others" (other branches in the batch),
"ran" (this process led), "doc"}`. `gave_up`: the batch runner died three times; the caller runs alone. Prints
"joined batch N with K other branches", "nothing is running here; starting at once", "batch N starts with ...". Locks: only
the short `locked` sections. Retry safety: a repeated call joins a fresh batch.

### `batch_queue:tree_parent`
`tree_parent(kind, repo_name, pid, start, suffix, environ)` returns the directory
`<HAMS_BATCH_TREE_DIR or ~/workspace/tmp>/batch_<kind>_<repo>_<pid>-<start><suffix>` that holds one combined tree
(`<parent>/<repo_name>`). The parent holds nothing else so the linters' "sibling beside the repository" lookup finds no
stray neighbour. The name carries the creator's pid and start time so `reap_orphan_trees` can tell an orphan.

### `batch_queue:build_combined_tree`
`build_combined_tree(repo, base, members, dest, run)` runs `git worktree add --detach dest base` from `repo`, then merges
each member's tip with `git merge --no-ff --no-edit` using a fixed identity (`hams-batch`), in order. A merge that fails is
aborted, the tree reset, and the member reported conflicted. Returns `{"merged": [members], "conflicted": [(member,
text)], "head": sha or None, "error": str (only when head is None)}`. Side effects: a new registered worktree (always remove
it with `remove_tree`); no network. Members sharing a tip are merged once.

### `batch_queue:remove_tree`
`remove_tree(repo, dest, run)` runs `git worktree remove --force`, deletes the parent directory (when files remain, because a test run as the odoo user
owns them, retries with the non-interactive `sudo -n rm -rf`) and runs `git worktree prune`. Safe to call twice and on a half-built tree.
Called in a `finally` by the runner.

### `batch_queue:reap_orphan_trees`
`reap_orphan_trees(repo, kind, repo_name, environ, live)` removes combined trees whose creator (pid and start time in the
directory name) is dead, and returns the removed parents. Runs at the start of every batch.

### `batch_queue:blame_by_tokens`
Attributes failed checks to branches. `failures` is `{name: output text}`; `owners` maps a token (a changed file path or a
module name) to the tips that own it; `find_tokens(line)` yields the tokens a line names. Returns `({tip: [name: line,
...]}, [names with no owned token])`. A failure naming at least one owned token is attributed to every owner of every token
it names; a failure naming none is unattributable and makes `settle` split.

### `batch_queue:settle`
Turns runs into verdicts. `settle(members, run_once, attribute, budget=9, log)`: `run_once(group)` returns a `RunResult`;
`attribute(failures, live_members)` returns what `blame_by_tokens` returns. Rules: exit 0 passes all; every failure
attributed: blamed tips fail, the rest pass at once; any unattributed failure (or none blamed): split the live members in
halves and settle each; a single member that fails unattributed fails with the failing step names; `fatal` fails everyone
with the run's exit code; conflicted members get `solo`; when `budget` runs are used up the rest fail with a note. Returns
`{tip: Verdict}`. Bound: about 2*log2(K)+1 runs for K members (red main on 8 members stops at the budget of 9).

### `batch_queue:format_verdicts`
`format_verdicts(doc, my_tip)` returns the printable lines: a header with batch id and log path, then one line per distinct
branch with its status in capitals, tip prefix, `<- you` for the caller and the detail of a non-pass.

## lint_batch

### `lint_batch:ineligible_reason`
Decides whether a run may join a batch. Keyword arguments: `changed` (the `--changed` base or None), `only`, `skip`,
`modules` (selection options), `solo`, `bundle`, `environ`, `dirty` (uncommitted changes), `is_shared_checkout`,
`lock_needed` (the run is heavy). Returns a short reason string when the run must run alone, None when it may batch.
Pure; checked in the order solo, runner marker, `HAMS_LINT_NO_LOCK`, lock held, `HAMS_LINT_BATCH=off`, not changed,
selection, bundle, dirty, hams_shared, cheap.

### `lint_batch:changed_paths`
`changed_paths(repo, base, tip)` returns the repository-relative paths in `git diff --name-only base...tip` (the merge-base
diff); `[]` if git fails. These are the tokens that own findings.

### `lint_batch:path_finder`
`path_finder(paths)` returns `find(line)` listing the paths a line of output names. A path matches only where the previous
character is not a file-name character, so `a.py` does not match `data.py`; absolute paths into the combined tree match by
their suffix. A line can name several owners' paths (a shared file); all are returned.

### `lint_batch:owners_of`
`owners_of({tip: [paths]})` returns `{path: [tips]}`, the `owners` argument of `blame_by_tokens`.

### `lint_batch:default_command`
`default_command(tree, results_json, passthrough, base)` returns the argv of the combined run: `nice -n 10 python3
<tree>/tools/run_linters.py --changed <base> --solo --results-json <path> <passthrough>`. `--solo` stops it batching again.
`passthrough` carries `-j`, `--lock-timeout`, `--load-limit`, `--load-timeout` of the leader's own command.

### `lint_batch:read_results`
`read_results(path)` reads the combined run's `--results-json` file: returns `(selected step names, {failed step: output})`;
`([], {})` if unreadable. The file's `selected` and `outputs` keys are added by `run_linters.py` (outputs are cut to the last
200000 characters per step).

### `lint_batch:LintBatch`
The leader's runner; an instance is the `run_batch` argument of `coalesce`.

| Field (constructor argument) | Meaning |
|---|---|
| `repo`, `repo_name` | a checkout or worktree of the repository for git commands; the tree directory name (`hams_com`) |
| `base` | ref the trees start from (the `--changed` base, `origin/main`) |
| `passthrough` | options copied to the combined run |
| `stamp_writer(tip, step_names, batch_id)` | writes the pass stamp of one passed tip (`run_linters.write_batch_stamp`) |
| `command(tree, results)` | argv of the run (tests substitute a fake linter) |
| `prepare(tree)` | links the tree for linting; default runs `fix_worktree_symlinks.py` when the tree has one |
| `out`, `environ`, `fetch` | printer, environment for the run, whether to `git fetch origin` first |

Class attributes `kind` ("lint"), `runner_env` (marker exported to the run) and the methods `collect(tree, results, output, code)`
(what a run reports: lint reads the results file) and `tokens(members)` (owners and finder) are what `OdooTestBatch` replaces;
`current_members` is the group of the run in progress. Calling the instance fetches, computes each member's changed paths, then `settle`s: each run builds a combined tree, runs the
command with output teed to the leader's terminal and the batch log, reads the results file and always removes the tree.
Stamps are written for every `passed` verdict from the step names of the last run. Exit code 75 from the run (lock or load
gate gave up) is `fatal` for the batch. Concurrency: only the leader runs this, and only after sealing.

### `lint_batch:run_member`
The caller's side, used by `run_linters.main`. `run_member(repo, repo_name, branch, tip, base, scope, runner, timeout, out,
environ, poll, sdir, estimate, kind, solo_hint)` (`kind` "lint" or "test" picks the queue and the wording, `solo_hint` is the command
quoted in the failure hint) joins through `coalesce`, prints the verdict lines, and returns the exit code: 0 passed, the
verdict's code (1) failed, 75 timed out. Returns None when the caller must run alone: the merge conflicted (`solo`) or the
runner kept dying (`gave_up`). For a failure it prints the findings naming the branch, the log path and a reminder that
`--solo` repeats the run alone.

## run_linters

### `run_linters:write_batch_stamp`
`write_batch_stamp(tip, step_names, batch_id)` writes `~/.cache/hams-lint/passed/<tip>` in the format of `stamp_text` plus
` batch=N` (mode `changed`, the number and names of the combined run's steps). `lint_stamp_gate.py` ignores the extra field.
Writes nothing, and says so on stderr, when `step_names` is empty (the gate refuses a stamp that selected zero steps).
Called only by the batch leader for tips whose verdict is `passed`.

## odoo_test_batch

The test side (`tools/odoo_test_batch.py`, hook in `tools/test.py`). Queue directory `state_dir("test", repo_key)`; pass records in
`~/.cache/hams-test/passed/<tip>`; recorded durations in `<state dir>/durations.json`.

### `odoo_test_batch:module_scope`
`module_scope(module_arg)` parses a `-u` value into a sorted, de-duplicated list of module names (a `hams_open/` prefix is dropped
as `test.py` does). Returns None when the run cannot be batched: empty or missing, an empty entry, or any entry with a `:Class`
selector.

### `odoo_test_batch:ineligible_reason`
`ineligible_reason(args, environ, cwd_is_root, dirty, has_base=True)`; `args` is test.py's parsed namespace; `has_base` is whether the repository has `origin/main` (`maybe_run_batched` checks with `git rev-parse --verify origin/main`). Returns a short reason when the run
must run alone, None when it may batch. Alone: `--solo`; the combined run itself (`HAMS_TEST_BATCH_RUNNER`); `HAMS_TEST_BATCH=off`;
a run inside a run (`HAMS_TEST_LOCK_HELD=1`); the internal re-exec phases; a mode other than standard; `--test-tags`, `--coverage`,
`--mcp`, `--pause-on-fail`, `--offline-isolation`, `--profile`, `--build-daemons`; no whole-module `-u` list; a directory that is not
the repository root; uncommitted changes; no `origin/main` to build the combined tree on (the plain copy `remote_odoo_test.py` syncs to a test host has none; batching it failed every remote run, 2026-11-04). Pure.

### `odoo_test_batch:implicated_modules`
`implicated_modules(text)` reads test.py's own `filtered_test.txt`: the `  - <module>` lines under "these modules are implicated in the
failure(s):". Returns the list (empty when the extractor named none: a crash, or a failure with no traceback).

### `odoo_test_batch:module_finder`
`module_finder(modules)` returns `find(line)` listing which of `modules` a line names as whole words (`ham_a` is not found in
`ham_ab`). The `find_tokens` argument of `blame_by_tokens` for tests.

### `odoo_test_batch:plan_machine_split`
`plan_machine_split(modules, durations, machines, default=300)` returns `{machine: [modules]}`: modules longest first (recorded
`durations` in seconds, `default` for an unrecorded one) onto the currently least-loaded machine (LPT, within 4/3 of optimal);
machines with nothing are omitted; one or no machine puts everything on that machine (or `"local"`). Pure. NOT YET USED by the
leader, which runs everything locally; it is the planned hook for `remote_odoo_test.py`.

### `odoo_test_batch:run_on_machine`
STUB. `run_on_machine(machine, tree, modules, log_directory)` always raises `NotImplementedError`. It marks where the leader will
run a share of the union on bruce-yoga or mac1 through hams_com's `devbox_tools/remote_odoo_test.py` (which syncs a trimmed tree
plus a hams_open checkout and has its own per-host lock). Needs a change in hams_com and a per-machine queue; see the to-do
`night_shift_todo/medium/split-test-batches-across-machines-*.md`.

### `odoo_test_batch:estimate_seconds`
`estimate_seconds(modules, durations, default=300)` is the sum of recorded durations, `default` per unrecorded module. The `estimate`
passed to `join` so a batch stops growing at `HAMS_BATCH_MAX_SECONDS`.

### `odoo_test_batch:load_durations`
`load_durations(sdir)` returns `{module: seconds}` from `<sdir>/durations.json`; `{}` when missing, unreadable, or when entries are
not finite numbers (those entries are dropped).

### `odoo_test_batch:record_durations`
`record_durations(sdir, modules, seconds)` credits each module an equal share of the run's wall time (test.py reports no per-module
time, so the figure is crude) and averages it with an earlier record; written atomically; ignores a failed write, empty modules
or non-positive seconds.

### `odoo_test_batch:write_pass_record`
`write_pass_record(tip, modules, batch_id, environ)` writes `~/.cache/hams-test/passed/<tip>` with one line
`tests <time> modules=a,b batch=N` (the modules the combined run covered) and returns the path; None when `modules` is empty or the
write fails. Nothing reads these records yet (prmerge.sh gates on lint only); they are the evidence a later gate can use.

### `odoo_test_batch:OdooTestBatch`
`LintBatch` subclass for tests (see `lint_batch:LintBatch` for the shared fields). Extra constructor argument `sdir` (durations
and queue directory). Each run: `python3 <tree>/tools/test.py -u <union of the scopes of the merged members> --solo --wait -l
<parent>/logs <passthrough: -d, -c, --wait-timeout>`; the log directory is created world-writable because test.py may run as the
odoo user; `filtered_test.txt` from it is appended to the batch log. Owners are modules (`tokens`): the members whose `-u` list names
a module own it. `collect`: exit 0 passes the union; otherwise each implicated module becomes a failure naming its lines; no implicated
module means one failure `test.py` with the text tail, which is unattributable and splits the batch. Records durations after every run.
Concurrency: leader only; the box-wide test lock is taken by the nested test.py exactly as for a single run.

### `odoo_test_batch:maybe_run_batched`
`maybe_run_batched(args, environ=None, cwd=None, out=print)`, called by `test.py`'s `main()` right after the argument check and
before any lock. Reads git for the repository root, dirty state, tip and branch; returns None when the run goes on alone (ineligible,
printing "test batch: running alone (<reason>)" for a `-u` run, a merge conflict with another branch, a batch runner that kept dying),
else blocks until this caller's verdict and returns its exit code (0, 1, or 75 when `--wait-timeout` ran out waiting). Side
effects: queue state under `~/.cache/hams-lint/batches/test-<repo>`, a temporary worktree under `~/workspace/tmp` in the leader.
