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
| `install` | list of str | tests only, optional (the key is absent when empty, and an older member never has it): modules the hosts must INSTALL for this member's tests but must not test (test companions). Never in the duration estimate or the split |

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
`lock_needed` (the run is heavy), `foreign_shared` (optional, default False: the tree's `hams_shared` is not the main
checkout's). Returns a short reason string when the run must run alone, None when it may batch.
Pure; checked in the order solo, runner marker, `HAMS_LINT_NO_LOCK`, lock held, `HAMS_LINT_BATCH=off`, not changed,
selection, bundle, dirty, hams_shared, foreign hams_shared, cheap.

### `lint_batch:foreign_shared_checkout`
`foreign_shared_checkout(tree_root, main_root) -> bool`: True when `<tree_root>/hams_shared` and `<main_root>/hams_shared`
resolve (realpath) to different directories. The batch's combined tree links `hams_shared` to the main checkout's, so a
branch whose own `hams_shared` was linked by hand (a hams_shared worktree holding the change under test) would be linted with
main's tools and passed with a stamp; such a run goes alone. False when either `hams_shared` is missing. Test:
`ForeignSharedTests`.

### `lint_batch:changed_paths`
`changed_paths(repo, base, tip)` returns the repository-relative paths in `git diff --name-only base...tip` (the merge-base
diff); `[]` if git fails. These are the tokens that own findings.

### `lint_batch:path_finder`
`path_finder(paths)` returns `find(line)` listing the paths a line of output names. A path matches only where the previous
character is not a file-name character, so `a.py` does not match `data.py`; absolute paths into the combined tree match by
their suffix. A line can name several owners' paths (a shared file); all are returned.

### `lint_batch:changed_anchors`
`changed_anchors(repo, base, tip) -> set[str]`: the anchor ids written on lines the branch added or removed, read from
`git -C repo diff -U0 base...tip` (the merge-base diff). A line counts when it starts with `+` or `-` after the file header
(a content line that begins with dashes is not mistaken for a header); every anchor tag on it contributes its id (base, `Tests`,
`Verified by`, `Triggers`, and the `-BEGIN`/`-END` forms). An anchor on an unchanged line is not included, so a branch is
only blamed for anchors it touched. Returns an empty set when git fails (bad ref); it never raises. It runs one `git diff` per
member of the batch and has no side effects. Why: `verify_anchors` and `check_function_test_anchors` print anchor ids, not
file names, so before this a failure there named no changed path, was unattributable and split the batch (b47d90e3). Test:
`test_changed_anchors_are_the_ids_on_added_and_removed_lines_only`.

### `lint_batch:anchor_finder`
`anchor_finder(ids)` returns `find(line) -> [ids]` for `blame_by_tokens`: the entries of `ids` that a line of output names as
a whole word. A word is a run of letters, digits, `_` and `:`; surrounding quotes, brackets and punctuation and a trailing
`:` are ignored, so `'m:feature'` and `m:feature:` both find `m:feature` while `xm:feature` and `m:feature_two` do not. Each id
is reported once per line. Test: `test_anchor_finder_finds_whole_anchor_ids_in_checker_output`, and
`test_a_failure_that_names_only_an_anchor_is_blamed_on_the_branch_that_changed_it` for the whole path through
`LintBatch.tokens` and `blame_by_tokens`.

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
(what a run reports: lint reads the results file) and `tokens(members)` (owners and finder; for lint the owners are the changed file paths, plus the anchor ids on changed lines) are what `OdooTestBatch` replaces;
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

### `odoo_test_batch:union_of`
`union_of(members, field)` returns the set of module names found in `field` ("scope" or "install") across `members`. A member without the key (queued before `install` existed, or with no companions) contributes nothing, which is the backward-compatibility rule for the queue's state file. Pure. Test: `test_union_of_reads_a_field_and_ignores_members_without_it`.

### `odoo_test_batch:ineligible_reason`
`ineligible_reason(args, environ, cwd_is_root, dirty, foreign_shared=False)`; `args` is test.py's parsed namespace; `foreign_shared` is True when the tree's `hams_shared` is not the main checkout's (`lint_batch:foreign_shared_checkout`). Returns a short reason when the run
must run alone, None when it may batch. Alone: `--solo`; the combined run itself (`HAMS_TEST_BATCH_RUNNER`); `HAMS_TEST_BATCH=off`;
a run inside a run (`HAMS_TEST_LOCK_HELD=1`); the internal re-exec phases; a mode other than standard; `--test-tags`, `--coverage`,
`--mcp`, `--pause-on-fail`, `--offline-isolation`, `--profile`, `--build-daemons`; no whole-module `-u` list; a directory that is not
the repository root; uncommitted changes; a `hams_shared` linked by hand to something other than the main checkout's (the combined tree links the main one, so the branch would be tested with main's helpers and pass). Pure.

### `odoo_test_batch:main_checkout_root`
`main_checkout_root(repo) -> str`: the main checkout of the git repository `repo` belongs to, the parent of `git rev-parse --git-common-dir`, so a worktree nested anywhere resolves to the real checkout; `repo` itself when git cannot say. `maybe_run_batched` passes it to `lint_batch.foreign_shared_checkout`.

### `odoo_test_batch:implicated_modules`
`implicated_modules(text)` reads test.py's own `filtered_test.txt`: the `  - <module>` lines under "these modules are implicated in the
failure(s):". Returns the list (empty when the extractor named none: a crash, or a failure with no traceback). A later section ends the list, and a second header (the texts of two hosts joined) starts another.

### `odoo_test_batch:failing_tests`
`failing_tests(text) -> set[str]` reads a `filtered_test.txt` and returns the failing tests as ids `"<module>:<Class>.<method>"`. It reads Odoo's logger form
(`odoo.addons.<module>.tests.<file>: ERROR: Class.method`, also `FAIL:`) and the unittest form (`ERROR: setUpClass (odoo.addons.<module>.tests.<file>.Class)`,
also `test_x (...)`, `tearDownClass`). A failure is listed twice in the file and counts once. Empty when no failing test is named (a crash, a module-load
error). Pure. Used by `RemoteTestBatch` to compare a host's failures with its origin/main baseline and to blame the module of each new failure.
Test: `test_failing_tests_reads_both_line_forms_and_counts_a_failure_once`.

### `odoo_test_batch:module_finder`
`module_finder(modules)` returns `find(line)` listing which of `modules` a line names as whole words (`ham_a` is not found in
`ham_ab`). The `find_tokens` argument of `blame_by_tokens` for tests.

### `odoo_test_batch:plan_machine_split`
`plan_machine_split(modules, durations, machines, default=300)` returns `{machine: [modules]}`: modules longest first (recorded
`durations` in seconds, `default` for an unrecorded one) onto the currently least-loaded machine (LPT, within 4/3 of optimal);
machines with nothing are omitted; one or no machine puts everything on that machine (or `"local"`). Pure. Used by `RemoteTestBatch` to split a batch over the idle test hosts.

### `odoo_test_batch:idle_machines`
**Purpose.** Tells the remote leader which test hosts are free right now, so a batch is split only over hosts that can start at once.

| Argument | Type | Meaning |
|---|---|---|
| `machines` | list of str | host names (`yoga`, `mac1`), in preference order |
| `lock_dir` | str or None | directory of the per-host lock files; default `~/.cache/hams-remote-test` (the files `remote_odoo_test.py` locks) |

**Returns.** The names whose lock could be taken without waiting, in the given order.
**Special concerns.** Takes each lock non-blocking and releases it at once, so the answer may be stale when the run starts (the driver then waits for the lock, `--wait`). Creates the directory and empty lock files. Never blocks.
**See also.** `RemoteTestBatch`, `remote_odoo_test.py host_is_idle` (same probe in hams_com). Test: `test_idle_machines_skips_a_host_whose_lock_is_held`.

### `odoo_test_batch:run_on_machine`
**Purpose.** Runs one host's share of a test batch. The host has its own clone of the repository and builds `origin/main` plus every member's commit itself; nothing is copied.

| Argument | Type | Meaning |
|---|---|---|
| `machine` | str | host name known to `remote_odoo_test.py` |
| `tree` | str | the leader's checkout (commits are fetched by the host from GitHub, or pushed to it over ssh from here) |
| `repo_name` | str | `hams_com` or `hams_open`: the repository under test |
| `members` | list of Member | the batch's members; only `branch` and `tip` are used, a repeated tip is sent once |
| `modules` | list of str | this host's share of the union: the modules whose TESTS it runs |
| `union` | list of str or None | every module of the batch; the host INSTALLS/UPDATES all of them (default: `modules`) |
| `driver` | list of str | argv prefix of `remote_odoo_test.py` |
| `log_path` | str | the host run's log |
| `work_dir` | str | existing directory for the members file and the result file |

**Returns.** The driver's result: `{"host", "exit_code", "merged": [sha], "conflicted": [sha], "filtered": text of filtered_test.txt, "log", "seconds", "error", "durations": {module: seconds} (optional: the host's per-module test times)}`. A driver that crashes or writes no result gives `exit_code` 75 and `error` set: the run could not start, which is not a test failure.
**Special concerns.** The driver is called with the union as its module list and `--share <modules>` when the share is smaller, so the host runs `test.py -u <union> --test-tags /m1,/m2`: splitting divides the tests, never the installed set. Blocks for the whole host run (minutes to an hour). Takes the host's lock inside the driver (`--wait`). Deletes a stale result file first. The host run is `test.py --solo` as the odoo user against `hams_test`.
**See also.** `RemoteTestBatch`; hams_com `devbox_tools/remote_odoo_test.py`, `devbox_tools/testhost_git.py`. Test: `test_run_on_machine_calls_the_driver_and_reads_its_result`.

### `odoo_test_batch:RemoteTestBatch`
**Purpose.** The leader's runner for batches tested on the remote hosts. An `OdooTestBatch` whose run goes to machines.

| Constructor argument | Type | Meaning |
|---|---|---|
| `repo`, `repo_name`, `base`, `passthrough`, `stamp_writer` | as `LintBatch` | `base` is `origin/main` |
| `sdir` | str | the queue directory (durations are kept there) |
| `machines` | list of str | hosts it may use, in preference order |
| `driver` | list of str | argv prefix of `remote_odoo_test.py` |
| `work_base` | str or None | parent of the per-run scratch directory |
| `probe` | callable | `idle_machines`-like; replaced in tests |
| `runner` | callable | `run_on_machine`-like; replaced in tests |

**Install-only modules.** A member's `install` modules (test companions such as another module's service account) are added to the installed set that every host gets (`-u`), but only the `scope` union is split over the hosts and tested (`--share`), and only it is the run's `selected` list. A module that one member installs and another tests is tested. The batch log says `installed on every host, not tested: ...`. Test: `test_install_only_modules_are_installed_on_every_host_but_never_tested`, `test_a_module_one_member_installs_and_another_tests_is_tested`.
**Per run.** The union of the group's modules is split over ALL the hosts by recorded duration (`plan_machine_split`); a host that is busy (`probe` finds its lock held) still gets its share, labelled "busy now" in the batch log, and its driver waits for the lock (Bruce 2026-11-05, "Combine tests": batch 31 put 24 modules on yoga alone because the others were busy for a few minutes); one thread per host calls `runner`; each host's log tail is appended to the batch log. Results are merged: a host that could not start makes the run fatal (exit 75, every member of the group fails with `run on X could not start`); a merge conflict (the hosts all build the same tree) becomes a `conflicted` member and its verdict is `solo`; a nonzero exit is read as failing tests (`failing_tests`): those that also fail in the host's origin/main baseline run (`baseline_filtered`, run by the driver after a failing batch run) are environmental, are listed in the batch log and do not fail any branch (a passing verdict carries a `note`); each other failing test is blamed on its module's owners; a failure naming no test falls back to the modules `filtered_test.txt` implicates, none implicated is unattributable and splits the batch. A clean run passes every branch in it (Bruce's rule) and writes the pass record of each (`write_pass_record`).
**Durations.** Recorded per host from that host's wall time over its own modules.
**Concurrency.** `_run_once` is called by the leader only; threads write only their own slot under a lock; log output is written after the threads join.
**See also.** `run_remote_member`, `OdooTestBatch`, `LintBatch`. Tests: `test_a_batch_is_split_over_the_hosts_and_each_branch_gets_its_own_verdict`, `test_a_clean_combined_run_marks_every_branch_passed`, `test_a_host_that_cannot_start_is_exit_75_not_a_test_failure`, `test_a_conflict_found_by_the_hosts_sends_that_branch_to_run_alone`.

### `odoo_test_batch:run_remote_member`
**Purpose.** The caller's side of a remote test run (`remote_odoo_test.py`): join the repository's `rtest` queue and block until this branch's verdict.

| Argument | Type | Meaning |
|---|---|---|
| `tree` | str | committed checkout or worktree of the repository (its main checkout names the queue) |
| `repo_name` | str | `hams_com` or `hams_open` |
| `branch`, `tip` | str | name and commit of the branch under test |
| `modules_arg` | str | comma-separated whole-module list; a `:Class` selector raises `ValueError` |
| `machines` | list of str | hosts the leader may use |
| `driver` | list of str | argv prefix of `remote_odoo_test.py` |
| `wait`, `timeout`, `environ`, `out` | | `timeout` seconds bound the wait; the queue always waits |
| `install` | list of str | modules to install on every host for this branch's tests without testing them (stored as the member's `install`); default none |

**Returns.** The exit code: 0 passed, 1 failed, 75 could not run or gave up; `None` when the branch must run alone (its merge conflicts with another branch, or the runner kept dying).
**Special concerns.** Blocks for minutes to an hour. The queue is `state_dir("rtest", repo_key)`, separate from the local `test` queue. Prints the batch verdicts. Writes `~/.cache/hams-test/passed/<tip>` for each passed branch (leader).
**See also.** `lint_batch.run_member`. Test: `test_run_remote_member_end_to_end_with_a_stand_in_driver`.

### `odoo_test_batch:estimate_seconds`
`estimate_seconds(modules, durations, default=300)` is the sum of recorded durations, `default` per unrecorded module. The `estimate`
passed to `join` so a batch stops growing at `HAMS_BATCH_MAX_SECONDS`.

### `odoo_test_batch:load_durations`
`load_durations(sdir)` returns `{module: seconds}` for the repository's test queue: the crude equal-share table
`<sdir>/durations.json` overlaid with the measured table `<sdir>/module_durations.json` (`record_module_durations`), so a module with
a measured time uses it and any other module its crude figure. `{}` when both are missing or unreadable; entries that are not finite
numbers in [0, 1e9) are dropped. `plan_machine_split` and `estimate_seconds` read this table.

### `odoo_test_batch:record_durations`
`record_durations(sdir, modules, seconds)` credits each module an equal share of the run's wall time (the crude figure, used only when a
host reported no per-module times) and averages it with an earlier crude record in `<sdir>/durations.json`; written atomically; ignores
a failed write, empty modules or non-positive seconds. It never reads or changes a measured figure.

### `odoo_test_batch:record_module_durations`
`record_module_durations(sdir, durations)` stores the measured per-module test times of one host's run in `<sdir>/module_durations.json`.
`durations` is `{module: seconds}` as the host's `odoo.tests.stats: <module>: N tests <s>s` lines report them
(`remote_odoo_test.module_test_seconds` in hams_com's devbox_tools, returned by `drive_host` as `result["durations"]`; `RemoteTestBatch`
passes each host's figures here). The first measurement of a module is stored as it is; a later one is averaged 50/50 with the stored
figure because the hosts differ in speed. Entries whose module is not a non-empty string or whose seconds are not a finite number in
[0, 1e9) (including booleans) are ignored; an empty mapping writes nothing; the file is written atomically and a failed write is
ignored. The time is Odoo's test time for the module, not its install time.

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

## lint_exec

### `lint_exec:_main_submodule_gitdir`
`_main_submodule_gitdir(common) -> str`: given the git common directory of a checkout, returns the git directory of the MAIN
checkout's submodule when `common` has the form `<parent>/.git/worktrees/<worktree>/modules/<name>` (a submodule initialised
inside a linked worktree of its parent) and `<parent>/.git/modules/<name>` exists; otherwise returns `common` unchanged. Used
by `main_worktree_of`, whose `core.worktree` lookup would otherwise name the worktree's own submodule directory, so a hams_open
worktree with `hams_shared` checked out inside it found no `node_modules` and ESLint was "not installed". Pure string work plus
one `isdir`. Test: `MainWorktreeTests.test_the_main_worktree_of_a_submodule_checked_out_in_a_parent_worktree_is_the_main_submodule`.

### `lint_exec:has_odoo_module`
`has_odoo_module(path) -> bool`: True when a directory directly under `path` holds an Odoo `__manifest__.py`; False for a
missing or unreadable `path`. It is the `accept` argument that the module-scanning checkers (`check_dead_code`,
`check_dependency_cycles`, `check_model_extension_collisions`, `odoo_registry_builder`, and the others listed in
`test_sibling_repo_checkers.py`) hand to `lint_exec.sibling_repo_root` to take the other repository by identity instead of
by directory name. Test: `SiblingByIdentityTests.test_the_accept_tests_look_for_a_module_and_for_agents_md`.

### `lint_exec:has_agents_md`
`has_agents_md(path) -> bool`: True when `path` holds an `AGENTS.md`. The `accept` argument of `check_doc_images`, which scans
manuals rather than modules. Same test as `lint_exec:has_odoo_module`.

