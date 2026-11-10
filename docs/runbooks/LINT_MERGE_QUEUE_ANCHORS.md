# Known-red baseline and merge queue: anchor reference (ADR 0113)

Operating manual: `LINT_MERGE_QUEUE.md`. Design: `docs/adrs/0117_known_red_baseline_and_merge_queue.md`.

## lint_baseline

### `lint_baseline:BaselineFile`
**Purpose.** The record of what fails on main: `~/.cache/hams-lint/baseline/<repository>.json`, one per repository, written
by the canary (`lint_baseline:refresh`) and read by every branch run (`lint_baseline:judge`).

| Field | Type | Meaning |
| --- | --- | --- |
| `version` | int | `1`; a file with another version is ignored. |
| `repo` | str | `hams_com`, `hams_open` or `hams_shared`. |
| `main` | str | The main commit the canary linted (full sha). |
| `created` | str | Local time of the canary run, ISO format. |
| `items` | object | `{item_id: item}`; empty when main was green. |

An item: `step` (step name without its shard suffix), `test` (test id, or `""` for a whole step), `signature` (12 hex
digits: hash of the normalized message), `message` (normalized, at most 240 characters), `since` (the oldest main commit at which
the item was seen red with this signature), `first_seen_time` (local ISO time it was first recorded; a record from before the field existed counts from `created`), `owner` (who fixes it, default `unassigned`) and optionally `renewed` (`{time, reason}`). An item id is `<step>::<test id>` or `<step>`.
An item is tolerated for `max_age_days` (default 3) from its last renewal; after that it blocks (see `classify`).
**Invariants.** `since` never moves forward while the item stays red with the same signature. **Lifetime.** Replaced whole,
atomically, at each canary run; never edited by hand and never committed. **Concurrency.** Written under a non-blocking lock
(`<repository>.lock`) so one canary runs per repository; readers need no lock because the file is replaced atomically.
**Used by.** `read_baseline`, `write_baseline`, `classify`, `judge`, `show_lines`.

### `lint_baseline:enabled`
`enabled(environ=None) -> bool`. True unless `HAMS_LINT_BASELINE` is `off`, `0`, `no` or `false`. Pure. `--no-baseline` sets the variable for the run and for the
runs it starts.

### `lint_baseline:baseline_dir`
`baseline_dir(environ=None) -> str`. `$XDG_CACHE_HOME` (else `~/.cache`) + `/hams-lint/baseline`. Pure; the directory is created by the writer.

### `lint_baseline:baseline_path`
`baseline_path(key, environ=None) -> str`. The file of repository `key` inside `baseline_dir`. Pure.

### `lint_baseline:normalize_message`
**Purpose.** Makes a failure message comparable between runs. `normalize_message(text) -> str` replaces absolute paths (and a
`:line` after one), `0x` addresses, hex strings of 8 or more digits, durations (`1.5s`, `20 ms`) and whitespace runs, then strips.
Other digits are kept, so `3 != 4` and `3 != 5` differ. Pure; no errors.

### `lint_baseline:cargo_panic_message`
**Purpose.** The message of a failing cargo test, so a test red on main that fails differently on a branch is not tolerated.
`cargo_panic_message(lines, test) -> str` returns the lines of the `---- test stdout ----` block up to the first blank line, with the
`thread '..' panicked at file:line:col:` header reduced to its message (no line number, so unrelated edits do not change it). Returns ""
when the output has no such block. Pure; no errors.

### `lint_baseline:failure_items`
**Purpose.** Turns one failed step's output into items. `failure_items(step_name, output) -> {item_id: item}`.

| Argument | Type | Meaning |
| --- | --- | --- |
| `step_name` | str | The step's name; a `[...]` suffix (shard) is dropped from the item id. |
| `output` | str | The step's whole output. |

Returns one item per pytest `FAILED`/`ERROR` line, unittest `FAIL:`/`ERROR:` block or cargo `... FAILED` line; when none is found, one
item named for the step whose signature is the hash of the sorted, normalized, non-progress output lines (the same findings give the same signature; one more finding changes it).
**Special concerns.** Pure, no I/O, no errors. A step that crashes the same way every time gets the same signature, so a recorded crash is tolerated only while it
stays identical. **See also.** `items_from_failures`; tests in `tools/test_lint_baseline.py`.

### `lint_baseline:items_from_failures`
`items_from_failures({step name: output}) -> {item_id: item}`: `failure_items` of every failed step, merged. Pure.

### `lint_baseline:read_baseline`
`read_baseline(key, environ=None) -> dict | None`. The document, or None when the file is missing, not JSON, not an object, of another `version`, or has no
`items` object. Never raises.

### `lint_baseline:write_baseline`
**Purpose.** Records a canary result. `write_baseline(key, main, items, environ=None, previous=None, now=time.time) -> dict`.

| Argument | Type | Meaning |
| --- | --- | --- |
| `key` | str | Repository name. |
| `main` | str | The main commit the canary linted. |
| `items` | dict | `{item_id: item}` from `items_from_failures`. |
| `previous` | dict or None | The old document: an item with the same id and signature keeps its `since`, `first_seen_time`, `owner` and `renewed`; others get `since=main`, `first_seen_time` now and owner `unassigned`. |

Returns the document written. Raises `OSError` when the directory or file cannot be written. Writes a temporary file in the same directory and renames it (atomic).

### `lint_baseline:classify`
**Purpose.** The decision rule. `classify(items, doc, repo=None, head="HEAD", contains=..., now=time.time, environ=None) -> (tolerated, blocking)`.
`tolerated` maps an id to the recorded item when the id is recorded, the signature is equal and (when `repo` is given) `git merge-base --is-ancestor <since> <head>`
holds and the item is not older than `max_age_days` (counted from the last renewal; each tolerated item is returned as a copy with `age_days`). `blocking` maps an id to a reason: `new: not failing on main`, `changed: fails differently from main (main: ...)`,
`main was red since <sha>, which is not in your tree: rebase on origin/main`, or `known red for N days (limit L days, owner O): fix it, or renew it with a reason`. `doc=None` tolerates nothing. Runs `git` once per tolerated candidate. Pure otherwise.

### `lint_baseline:judge`
**Purpose.** The verdict of one branch run. `judge(failed, key, repo, head="HEAD", environ=None, now=time.time) -> dict` with `tolerated`, `blocking`, `items`, `main` and `passes`.
`failed` is `{step name: output}` of every failed step; `repo` is the git checkout whose history is searched. `passes` is True only when something failed and every failure is tolerated.
With no failures, no baseline file or the switch off, nothing is tolerated and `passes` is False. Called by `run_linters.main` after the steps ran; the result
decides the exit code, is written to `--results-json` (`known_red`) and goes into the pass stamp.

### `lint_baseline:report_lines`
`report_lines(verdict) -> [str]`. The loud block: a `KNOWN RED ON MAIN` header with one `known red:` line per tolerated item (id, since, message), and, when
something still blocks, a header and one `BLOCKS:` line per blocking item with its reason. Pure.

### `lint_baseline:stamp_field`
`stamp_field(tolerated, main) -> str`. ` known_red=<id>(<age>d;<owner>),<id>(...) baseline=<main 12 hex>` appended to the pass stamp's line (age in days from the item's `age_days`, owner from the baseline); `""` when nothing was tolerated. Spaces in ids become `_`;
more than 12 ids end with `+N`. `lint_stamp_gate.known_red_note` reads it back.

### `lint_baseline:refresh`
**Purpose.** The canary. `refresh(repo, key, tree_name, base="origin/main", force=False, environ=None, out=print, remote="auto", command=None, fetch=True, prepare=None) -> int`.

| Argument | Type | Meaning |
| --- | --- | --- |
| `repo` | str | A git checkout (or worktree) of the repository; borrowed. |
| `key` | str | Baseline name. |
| `tree_name` | str | Directory name of the temporary tree (never `hams_shared`). |
| `base` | str | The ref to lint. |
| `force` | bool | Run even when the record is already for `base`. |
| `remote` | str | Passed as `--remote`: `auto` keeps the overflow routing (this machine first). |
| `command`, `prepare` | callables | Test hooks: the argv of the run, and the tree preparation. |

Returns 0 (recorded, already current, or another canary is running), 2 (cannot resolve `base` or build the tree), 75 (the run did not finish: exit 75 or no steps reported; the
old record is kept). **Special concerns.** Blocking subprocess, minutes long (a full lint run under `nice -n 10`); takes a non-blocking `flock`;
creates and removes a worktree under `~/workspace/tmp`; the run takes the machine's lint lock like any other; never touches main or any branch.

### `lint_baseline:show_lines`
`show_lines(key, environ=None, now=time.time) -> [str]`: the record, one line per item, oldest first, each with its age, whether it is `EXPIRED: blocks`, its owner, `since`, a renewal reason if any and the message; the header gives the age limit. A line saying none is recorded when there is no record. `--baseline-show`.

### `lint_baseline:max_age_days`
`max_age_days(environ=None) -> float`: how long an item is tolerated: `HAMS_LINT_BASELINE_MAX_AGE_DAYS` or 3. Raises `ValueError` for anything that is not a finite number above 0 (a quiet "forever" would defeat the limit). Pure.

### `lint_baseline:item_age_days`
`item_age_days(item, created=None, now=time.time) -> float`: days since the item's last renewal, else its `first_seen_time`, else `created` (the document time, for a record from before the field). 0.0 when no readable time exists; never negative. Pure.

### `lint_baseline:renew`
`renew(key, item_id, reason, owner=None, environ=None, now=time.time) -> item`: keeps tolerating an item for another window. Stores `renewed: {time, reason}` (and `owner` when given) in the baseline file, atomically. Raises `ValueError` for an empty reason, `KeyError` when the repository has no baseline or the item is not in it. `run_linters.py --baseline-renew ITEM --baseline-reason TEXT [--baseline-owner NAME]`.

## run_linters

### `run_linters:baseline_command`
`baseline_command(options, dir_path, repo_root) -> int`. Handles `--baseline-refresh`, `--baseline-show` and `--baseline-renew` (exit 2 with the cause for an empty reason or an unknown item): picks the repository (a worktree of hams_shared is `hams_shared` with the tree named
`hams_shared_wt`; hams_com and hams_open by their main checkout's directory name), prints or refreshes. Returns 2 from the main hams_shared checkout. Takes no lock of its own.

## lint_batch

### `lint_batch:read_known_red`
`read_known_red(path) -> str`: the `known_red` field of a combined run's `--results-json` file, or `""`. The batch leader passes it to the stamp writer so every
passing member's stamp records what was tolerated.

## lint_stamp_gate

### `lint_stamp_gate:known_red_note`
`known_red_note(stamp_text) -> str | None`: the NOTE line that `lint_stamp_gate.py` prints (and so `prmerge.sh` shows) when a stamp carries `known_red=`.

## lint_exec (timeouts under load)

### `lint_exec:cpu_pressure`
`cpu_pressure(path="/proc/pressure/cpu") -> float | None`: the `some avg10` value (percent of the last 10 seconds in which some task waited for a processor), or None when the file is missing or unreadable. Reads one small file; never raises.

### `lint_exec:saturation`
`saturation(environ=None, loadavg=os.getloadavg, cpus=None, pressure=cpu_pressure) -> (bool, str)`. True when CPU pressure is at least `HAMS_LINT_TIMEOUT_PSI_PERCENT` (default 25) or the one-minute load average is at least `HAMS_LINT_TIMEOUT_LOAD_RATIO` (default 1.25) times the processor count; the string names the signal and its numbers, `""` when quiet. A non-numeric limit means the default. No locks.

### `lint_exec:timeout_scale`
`timeout_scale(environ=None, ...) -> float`: 1.0 when quiet, else load per processor clamped to 1..4. `run_step` exports it to every step as `HAMS_LINT_TIMEOUT_SCALE`; a checker with its own time limit multiplies by it (`check_pip_audit.py` does for its 300 s audit limit). `HAMS_LINT_TIMEOUT_SCALE_FIXED=1` pins it to 1. Time limits inside other steps (for example a test's own timeout) are not scaled; they are covered by the serial rerun.

### `lint_exec:timeout_suspect`
`timeout_suspect(result) -> bool`: the step failed, really ran (not a cache hit), the machine was saturated when it ended (`Result.saturated`, filled by `run_step` for failed steps only) and its output matches `TIMEOUT_TEXT` (timed out, timeout, did not complete/respond within, deadline exceeded). Pure.

## run_linters (timeouts under load)

### `run_linters:retry_timed_out_steps`
`retry_timed_out_steps(results, steps, execute=None, out=print) -> (results, [names])`. After the whole run, each `timeout_suspect` step is rerun once, alone and one at a time (`lint_exec.execute(step, 1)`). A pass replaces the failed result and is returned in the list of names; a second failure keeps the original failure and prints `failed again` (with the machine quiet it is a real failure). `HAMS_LINT_TIMEOUT_RETRY=off` disables it. Never loops; at most one extra run per candidate. The caller records the names in the pass stamp.

### `run_linters:timeout_retried_field`
`timeout_retried_field(names) -> str`: ` timeout-retried=<step>,<step>` (spaces in a name become `_`) or `""`. `lint_stamp_gate.known_red_note` prints it as a NOTE, so a merge shows what the pass rests on.
