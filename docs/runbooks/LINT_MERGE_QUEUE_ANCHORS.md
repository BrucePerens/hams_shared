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
the item was seen red with this signature). An item id is `<step>::<test id>` or `<step>`.
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
| `previous` | dict or None | The old document: an item with the same id and signature keeps its `since`; others get `since=main`. |

Returns the document written. Raises `OSError` when the directory or file cannot be written. Writes a temporary file in the same directory and renames it (atomic).

### `lint_baseline:classify`
**Purpose.** The decision rule. `classify(items, doc, repo=None, head="HEAD", contains=...) -> (tolerated, blocking)`.
`tolerated` maps an id to the recorded item when the id is recorded, the signature is equal and (when `repo` is given) `git merge-base --is-ancestor <since> <head>`
holds. `blocking` maps an id to a reason: `new: not failing on main`, `changed: fails differently from main (main: ...)` or
`main was red since <sha>, which is not in your tree: rebase on origin/main`. `doc=None` tolerates nothing. Runs `git` once per tolerated candidate. Pure otherwise.

### `lint_baseline:judge`
**Purpose.** The verdict of one branch run. `judge(failed, key, repo, head="HEAD", environ=None) -> dict` with `tolerated`, `blocking`, `items`, `main` and `passes`.
`failed` is `{step name: output}` of every failed step; `repo` is the git checkout whose history is searched. `passes` is True only when something failed and every failure is tolerated.
With no failures, no baseline file or the switch off, nothing is tolerated and `passes` is False. Called by `run_linters.main` after the steps ran; the result
decides the exit code, is written to `--results-json` (`known_red`) and goes into the pass stamp.

### `lint_baseline:report_lines`
`report_lines(verdict) -> [str]`. The loud block: a `KNOWN RED ON MAIN` header with one `known red:` line per tolerated item (id, since, message), and, when
something still blocks, a header and one `BLOCKS:` line per blocking item with its reason. Pure.

### `lint_baseline:stamp_field`
`stamp_field(tolerated, main) -> str`. ` known_red=<id>,<id> baseline=<main 12 hex>` appended to the pass stamp's line; `""` when nothing was tolerated. Spaces in ids become `_`;
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
`show_lines(key, environ=None) -> [str]`: the record, one line per item, or a line saying none is recorded.

## run_linters

### `run_linters:baseline_command`
`baseline_command(options, dir_path, repo_root) -> int`. Handles `--baseline-refresh` and `--baseline-show`: picks the repository (a worktree of hams_shared is `hams_shared` with the tree named
`hams_shared_wt`; hams_com and hams_open by their main checkout's directory name), prints or refreshes. Returns 2 from the main hams_shared checkout. Takes no lock of its own.

## lint_batch

### `lint_batch:read_known_red`
`read_known_red(path) -> str`: the `known_red` field of a combined run's `--results-json` file, or `""`. The batch leader passes it to the stamp writer so every
passing member's stamp records what was tolerated.

## lint_stamp_gate

### `lint_stamp_gate:known_red_note`
`known_red_note(stamp_text) -> str | None`: the NOTE line that `lint_stamp_gate.py` prints (and so `prmerge.sh` shows) when a stamp carries `known_red=`.
