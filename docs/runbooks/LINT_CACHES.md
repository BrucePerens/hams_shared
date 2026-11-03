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

## 3. Measurements (dev box, 16 cores, load 3-20 from other agents' runs; same inputs before and after)

`check_cargo_clippy` on the main checkouts of hams_open and hams_com (about 40 crates), one shared managed target
directory, `CARGO_INCREMENTAL=0`, one run after the other (`cargo_pingpong` experiment, 2026-11-03):

| Run | Time | Directory after |
|---|---|---|
| A1 cold, main checkouts | **889 s** | 3.8 GB |
| A2 same checkout again, old stale-output scan | **961 s** (the scan, not cargo) | |
| same, new scan (`check_cargo_clippy.py` in this change) | **19.6 s** under load, 5.7 s on the next run | |
| B1 another checkout path (a worktree set): 7 build-script outputs purged, their crates rebuilt | **280 s** | 3.8 GB |
| B2 same worktree again | 5.7 s | |
| A3 back to the main checkouts | 210 s (6 purged) | 3.8 GB |

Reading it. (1) The 1,100-1,250 s of the earlier logs are real: a cold clippy of both repositories is 900-1,250 s. An earlier
measurement in this work (106-129 s cold) was taken on a smaller crate set and is not representative; ignore it.
(2) Warm runs were dominated by `purge_stale_build_script_output`, which read every file of every `build/*/out` directory (the
object files of `*-sys` crates) and ran a regex over each, once per crate: 961 s on a 3.8 GB directory. It now reads each run
directory once and only the first 4 KiB of binary files. (3) The shared directory is fast while consecutive runs use the same
checkout path (5.7 s) and pays 210-280 s whenever the checkout alternates (ten agents, ten worktrees): build scripts whose output
names the other checkout's path are purged on purpose (bench_mcp's `include_str!` bug) and so are `opusic-sys` and
`hams_local_relay`, which rebuild C code. Still much better than cold (889 s). A cheaper rule for which outputs to purge, or a
second directory per checkout for only the `*-sys` units, is a follow-up (night_shift_todo/medium/lint-speed-remaining-costs).

Other steps:

| Step | Before | After | Notes |
|---|---|---|---|
| `check_float_isfinite` (both repositories) | 10.8 s | 0.2 s warm (13.7 s cold) | per-file result cache |
| `check_int_float_overflow` | 8.0 s | 0.1 s warm (8.3 s cold) | per-file result cache |
| `check_pip_audit` (5 requirements files) | 29.6 s | 21.1 s | files audited concurrently (`HAMS_PIP_AUDIT_JOBS`, default 4) |
| old shared cargo directory | 31 GB (26 GB incremental) | 3.8 GB after a cold build | |

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

* `check_burn_list`: **no per-file result cache, deliberately** (second pass, 2026-11-03). `scan_file` fills module-level cross-file
  tables (`FOUND_MANIFESTS`, `FOUND_TEST_CONTENTS`, `ACT_WINDOW_ACTIONS`, `REQUIRE_TEST_VERIFICATION`, ...) that the whole-tree passes
  at the end of `main()` read, so a cache has to store and replay every delta: a rewrite, not a cache. It was not needed. A
  `cProfile` of one scan (hams_open, 918 files) found 8 M `re.search` calls that were not regex work at all: for every line and
  every rule the scanner re-tested the rule's *file-path* pattern (`re.search(ext_pattern, filepath)`), which depends on the
  file only. Two mechanical changes, findings byte-identical (`cmp` of the full output on hams_open and hams_com, 577 unit tests):
  the applicable rules are chosen once per file, and the four whole-tree `ast.walk`s in the visitor's constructor share one walk.
  Measured, same inputs, same box, one run after the other (load 7-14 from other agents):

  | Scan | Before | After |
  |---|---|---|
  | hams_open | 33.5 s (load 8.6) | 13.2 s (load 13.7); 6.3 s (load 7.6) |
  | hams_com | 258 s (load 14) | 36.9 s (load 12); 23.0 s (load 6.6) |
  | cProfile, hams_open | 83 s | 21.7 s |

  What is left (profile): 3.8 M real regex searches 3.1 s, the visitor's AST traversal about 6 s, `compile` 1.7 s. That is below
  `verify_anchors` (16-21 s), so the Rust rewrite (`regex::RegexSet`) is not justified; the evidence for it would be a profile
  where the regex engine is the majority, and it is 15 %.
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
