# Lint caches: warm Cargo builds, per-file results, disk budgets

Written 2026-11-03 (lint speed work). Code: `tools/lint_cache.py`; budgets: `cache_budgets` in `tools/lint_machines.json`.

## 1. Warm Cargo build directory

`check_cargo_clippy` compiles every dependency of every crate. In a fresh `target/` that is the whole cost; in a
warm directory it is the crates of the checkout only.

* **One directory per repository and toolchain per machine**, never per worktree or branch:
  `~/.cache/hams-lint-cargo/<repo>-<rustc release>-<8 hex of rustc -vV>` (`hams_open-1.98.1-778fbc64`).
  Registry dependencies are keyed by version and features, not by checkout, so every worktree reuses them; only the
  workspace's own crates (whose unit hash contains the path) are rebuilt in a new worktree.
* **Incremental compilation is off** (`CARGO_INCREMENTAL=0`) for these builds. Measured on the old shared directory
  (`~/.cache/hams-cargo-target-lint`, 2026-11-03): **31 GB, of which `incremental/` 26 GB** (1,448 sessions, one per
  worktree path, never reused), dependencies 4 GB, build scripts 2.5 GB.
* **Lock.** `<dir>.lock` (beside the directory) is held exclusively for prune + all builds of the step, so a prune
  never runs under a live build. Cargo's own lock on the directory protects every other cargo command.
* **Budget, enforced before every build** (`lint_cache.prepare_cargo_target`): if the directory is over `cargo_gb`, or
  the filesystem has less than `free_floor_gb` free, prune; if that is not enough, the directory is cleared and the
  build is cold. A run never fails for lack of space. Pruning removes incremental sessions first, then whole
  compilation units (every `deps/`, `.fingerprint/`, `build/` file of one unit hash together), least recently used
  first (max of atime and mtime), until the directory is at 80 % of the budget. A directory whose repository or
  toolchain has not been used for 14 days is removed.
* **First use** on a machine renames the old single shared directory to the new one (if no cargo is building in it),
  so its dependencies stay warm; its incremental data is pruned first.
* **Log.** The step prints one line (`lint cache: cargo target <dir> X GB of Y GB budget, Z GB free (floor F GB)`) and a
  line for every prune, clear or removal.
* **Bypass.** `run_linters.py --no-cache` (or `HAMS_LINT_NO_CACHE=1`) builds in a throwaway directory deleted after the
  step. A `CARGO_TARGET_DIR` set by the caller is used as it is and never pruned.

### Budgets

| Machine | `cargo_gb` (each directory) | `free_floor_gb` | `results_mb` | Why |
|---|---|---|---|---|
| devbox | 25 | 30 | 200 | /home is 88 % full |
| mac1-vm | 12 | 20 | 100 | 99 GB VM disk, hosts the production FCC sync |
| bruce-yoga | 40 | 20 | 200 | 192 GB disk, 143 GB free |

Change them in `cache_budgets` of `tools/lint_machines.json`, or per run with `HAMS_LINT_CARGO_GB`,
`HAMS_LINT_FREE_FLOOR_GB`, `HAMS_LINT_RESULTS_MB`. A runner finds its own entry through `hostnames` (or
`HAMS_LINT_MACHINE`).

### Looking at and clearing the caches

```
python3 tools/run_linters.py --lint-caches show|prune|clear        # this machine
python3 tools/lint_remote.py caches [HOST...] [--prune|--clear]     # runners (default: show)
```

## 2. Per-file result caches

`lint_cache.ResultCache(checker, version)` stores a checker's per-file result under
`~/.cache/hams-lint-results/<checker>/<version>.json`, keyed by the SHA-256 of the file's content. `version` is the
hash of the checker's own source, so editing the checker starts a new cache. A changed file has a different content
hash and can never read an old entry; the same content in two worktrees shares an entry. Writers merge under an flock
and replace the file atomically; the file is capped at `results_mb` (oldest entries dropped first). `--no-cache` /
`HAMS_LINT_NO_CACHE=1` neither reads nor writes it.

## 3. Measurements (dev box, 16 cores, load 3-8 from other agents' runs; same inputs before and after)

| Step | Before | After | Notes |
|---|---|---|---|
| `check_cargo_clippy`, cold directory (hams_open + hams_com crates) | 111 s, 1,224 MB (incremental on) | 106-129 s, 764 MB (`CARGO_INCREMENTAL=0`) | The 1,100-1,250 s in earlier logs were not reproduced on an idle box: a cold build here is about 2 minutes. Incremental made no difference to the cold time and 60 % more disk. |
| same, warm, no change | 2.8 s | 2.0-2.3 s | |
| same, warm directory, a different worktree path | not measured (old directory: dependencies shared, incremental sessions per path) | 19 s | only the workspace's own crates rebuild |
| `check_float_isfinite` (both repositories) | 10.8 s | 13.7 s cold (load), **0.2 s** warm | per-file result cache |
| `check_int_float_overflow` | 8.0 s | 8.3 s cold, **0.1 s** warm | per-file result cache |
| `check_pip_audit` (5 requirements files) | 29.6 s | 21.1 s | files audited concurrently (`HAMS_PIP_AUDIT_JOBS`, default 4) |
| old shared cargo directory | 31 GB (26 GB incremental) | budgeted, 0.7 GB per repository+toolchain after a cold build | |

Findings are identical: the clippy fixture crate with a real warning prints the same diagnostics cold and warm (only
Cargo's own `Checking` / `Finished ... in 0.05s` progress lines differ), and the two scanners' outputs are
byte-identical with and without the cache (`cmp` on the same trees; unit tests pin it).

## 3a. Test chunks balanced by measured time

`pytest tools/[NofM]` was split by file length. Measured per-file seconds (`tools/lint_test_durations.json`, from one
`pytest -n 4 --dist loadfile` run of all 108 files, 3,413 passed in 401 s at load 8-12) show the split by length gives
estimated chunk times of **195 / 188 / 458 / 351 s** (the 195 and 450-660 s of the logs); `lint_test_durations.balanced_chunks`
(longest first into the lightest chunk) gives **355 / 279 / 279 / 279 s**, so the slowest chunk drops from 458 to 355 s. The 355 s
is one file: `test_b2_backup.py` (real kopia repositories, 42 tests, four of them 35-59 s) is a floor under any split
(xdist `--dist loadfile` keeps a file on one worker). Coverage proof (`lint_step_inventory.py collect` before/after,
ids normalised to `tools/...`): 3,417 test ids before, 3,444 after, **0 lost, 0 duplicated**, the 27 new ones are the new tests
of this change. The argv of every `pytest tools/[NofM]` step changed, so each machine's step cache misses once.
`HAMS_LINT_CHUNKS=size` restores the old split. Refresh the table (command in `lint_test_durations.py`) after large changes.

## 4. Not cached, and why

* `check_burn_list` (19 s on an idle box; 44-287 s under load): `scan_file` fills module-level cross-file tables
  (`FOUND_MANIFESTS`, `FOUND_TEST_CONTENTS`, `ACT_WINDOW_ACTIONS`, `REQUIRE_TEST_VERIFICATION`, ...) that the whole-tree
  passes at the end of `main()` read. A per-file cache has to store and replay every one of those deltas; that is a
  rewrite of a 6,000-line checker, not a cache. The time is load-dependent, not algorithmic, so it is recorded as a to-do
  with this evidence and not changed here.
* `check_untyped_utility_files`: the result of mypy for a file depends on the modules it imports, so a content hash of
  the file alone is not a sound key. mypy's own incremental cache is the right tool; it is per-checkout path.
* `test_b2_backup.py` (355 s alone, see 3a) and `test_lint_lock.py` (190 s): single files that bound a chunk; profile them next.
* `eslint` (result cache): the type-aware rules (`no-floating-promises`) read the types of imported files, so ESLint's own result cache
  is not sound across a change to a dependency either. See the to-do.

## 5. Operations

* `lint cache: ... cleared the directory (this build is cold)` on EVERY run means the filesystem is below its floor or the
  budget is smaller than one build (a cold clippy needs about 0.8 GB per repository): free disk space or raise
  `free_floor_gb`/`cargo_gb` for that machine, otherwise every run pays the cold build.
* Checking what is stored: `python3 tools/run_linters.py --lint-caches show`; on the runners
  `python3 tools/lint_remote.py caches mac1-vm bruce-yoga`.
* The step estimate `COLD_COST_SECONDS["check_cargo_clippy"] = 600` in `lint_remote.py` still assumes a cold build; with
  a warm directory the step takes seconds, so it over-estimates the cost when routing. Follow-up.

## 6. ESLint in `--changed` mode

ESLint's time is per linted file with the type-aware config (hams_com, 2026-11-03, load 8-12): one file **8.8 s**, the
`daemons/shack_console` directory 93 s, the whole repository 70-79 s. A change outside every Odoo module (a daemon) hands
the per-module checkers the whole repository, so a one-file console change linted everything. `--changed` now gives ESLint
the changed scripts plus every file under the scoped directories that imports one (`run_linters.narrow_eslint_targets`:
a quoted import/require/from specifier whose last path part is the changed file's name, the directory name for `index`; a
superset of the real importers) with `--no-warn-ignored`. Measured on a one-file change to
`daemons/shack_console/console/src/pairing.js`: 4 files (it, two importers, one test), **11.2 s against 79.2 s**; same config,
same TypeScript project, same rules, so the findings of each linted file are the same: with `undefinedFnForLintTest();` and an unused constant
planted in that file, the narrowed and the whole-repository run print the same two errors for it (7.3 s against 48.8 s on that run).

Falls back to the whole directories (the old behaviour) for: a changed `eslint.config.js`, `tsconfig.json` or `package.json`; a
changed JSON file; no changed script; a changed script that is a classic script (no import/export/require: its globals are
shared with every file of the TypeScript program). `HAMS_LINT_ESLINT=whole` forces the old behaviour. A full run (no
`--changed`) is unchanged. Not narrowed: a dependent that uses a changed file through a global or a registry name and not an
import (Odoo `registry.category(...)` lookups carry no types), which the type-aware rules cannot see either.
