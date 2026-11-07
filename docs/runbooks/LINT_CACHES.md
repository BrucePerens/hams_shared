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
| mac1-vm | 12 | 10 | 100 | 99 GB VM disk with 17 GB free (2026-11-03), hosts the production FCC sync. The floor was 20: with 17 GB free every run was below it, pruned the directory to nothing and built cold (`lint cache: ... cleared the directory`), so the runner never had a warm Cargo directory. Two directories (hams_com 4.5 GB, hams_open 3.6 GB on arm64) leave about 9 GB for the VM, more than the sync needs |
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
`hams_local_relay`, which rebuild C code. Still much better than cold (889 s). Fixed in the second pass, next paragraph.

**Checkout switch, second pass (2026-11-03).** The purge rule is now narrower (`check_cargo_clippy.py`, `_reusable_here`). What
was purged on a switch, found by looking at what names the other checkout (two clean sparse worktrees of hams_com and hams_open
at the same commit, `daemons/` only; crates hams_local_relay, shack_console, bench_mcp, ham_digital_modes):

| Run directory | Names the other checkout in | Before | Now |
|---|---|---|---|
| `opusic-sys` | `out/build/CMakeCache.txt` and other CMake/ninja files (the script's private build tree) | purged, C rebuilt | kept: same sources, older than `invoked.timestamp` |
| `hams_local_relay` | the same, for its `rade_c` C sources | purged | kept under the same test |
| `shack_console` (x2) | `cargo:rerun-if-changed=<abs path>` | purged | kept under the same test |
| `bench_mcp` | `out/manual_files.rs` (`include_str!` of absolute paths) | purged | purged (the bug the purge exists for) |

Kept only when every match is of those two harmless kinds and the files they name exist, byte for byte, in this checkout, and no
file of either tree is newer than the run's `invoked.timestamp` (the time cargo itself compares: a changed file makes cargo rerun
the script, and a rerun over the other checkout's CMake cache would fail). A `cargo:` directive or a generated source naming the
other checkout is always purged. Backstop: if the build of a crate fails while some output was kept, the kept directories are
deleted and that crate is built once more. Same inputs, idle box (load 1-3), switch from checkout A to B on a copy of the
directory built from A: **old rule 58.4 s (5 run directories purged), new rule 15.9 s (1 purged)**; with no purge at all 46.6 s
(cargo itself rebuilds the workspace crates, and bench_mcp's stale `include_str!` file is the hazard). The 210-280 s of the
earlier measurement covered the whole repository pair at load 8-20. The rule only helps when both checkouts are older than the
last build, i.e. alternating between existing worktrees; a worktree created after the cached run has newer files, cargo reruns
those scripts itself, and they are purged as before. Pinned by `KeepOutputThatOnlyNamesAnotherCheckoutTests` (15 cases).

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
was one file: `test_b2_backup.py` (real kopia repositories, 42 tests, four of them 35-59 s), a floor under any split
(xdist `--dist loadfile` keeps a file on one worker). **Second pass:** the file is now four (`test_b2_backup.py`,
`_end_to_end`, `_tenant_archives`, `_qso`; same 42 tests, the shared fixtures imported from the first). Profile: every test
builds its own repository (`init` + `backup` in `setUp`, 28 kopia calls of 0.1-0.5 s each in the slowest test, 4.4 of its 7.2 s);
nothing sleeps, so there was nothing to remove without sharing state between tests, which would weaken their isolation. Run
alone and idle, the four files take 23 / 48 / 61 / 92 s (the slowest 92 s, against 240-355 s for the one file); the table in
`lint_test_durations.json` has the new files with the old total shared by those proportions. Coverage proof (`lint_step_inventory.py collect` before/after,
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
* `check_untyped_utility_files` (mypy per file; 16 s idle on the dev box, 5-212 s under load; on a lint runner 413 s on the first
  full run and 254 s on the next, at a machine load of 8): **not cached by result, deliberately.** The result of mypy for a file
  depends on every module it imports (local files, installed packages, stubs), so a content hash of the file alone is not a sound
  key; a sound key needs the hashes of the whole import closure plus the mypy and package versions, i.e. mypy's own dependency
  graph, which is what mypy's incremental cache already is. That cache is in effect where the checkout path is stable: each
  worker keeps `.mypy_cache/w<N>` in the runner's mirror, and the second full run on bruce-yoga was 40 % faster (413 s to 254 s). In
  `--changed` mode only the changed files are checked. A cache shared across worktrees is not possible (mypy's cache is keyed by
  path), and a result cache of our own was not built.
* `test_b2_backup.py` (split into four files, see 3a) and `test_lint_lock.py` (190 s): single files that bound a chunk.
* `eslint` (result cache): the type-aware rules (`no-floating-promises`) read the types of imported files, so ESLint's own result cache
  is not sound across a change to a dependency either. See the to-do.

## 5. Operations

* `lint cache: ... cleared the directory (this build is cold)` on EVERY run means the filesystem is below its floor or the
  budget is smaller than one build (a cold clippy needs about 0.8 GB per repository): free disk space or raise
  `free_floor_gb`/`cargo_gb` for that machine, otherwise every run pays the cold build.
* Checking what is stored: `python3 tools/run_linters.py --lint-caches show`; on the runners
  `python3 tools/lint_remote.py caches mac1-vm bruce-yoga`.
* The router's cost of `check_cargo_clippy` (`WARM_COST_SECONDS` in `lint_remote.py`) was `COLD_COST_SECONDS` = 600 s, wrong both ways:
  warm 6.9-20 s, checkout switch 16-60 s, cold 889-1,250 s (only the first run of a machine and toolchain, or after the budget
  cleared the directory). It is now the measured warm upper end, 60 s, and the recorded median is not used for this step (it is
  dominated by the cold history: 233-3,220 s in the 2026-11-02 cache files, 6.9-7.9 s since the warm directory).

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

## 7. Tests that time out under load

`test_verify_anchors.py` runs `verify_anchors.py` as a subprocess (10 s alone). With ten agents' lint runs on 16 cores (load 20) the
fixed 30 s limit failed three of them, while the script was only slow. The limit is now `_timeout(base)`: the base limit times
twice the 1-minute load per core, at least the base and at most 8 times it (30 s becomes 75 s at load 20 on 16 cores, 240 s at most),
so a script that really hangs still fails. Pinned by `SubprocessTimeoutFollowsLoadTests`. The same rule now bounds the wait for
a loopback reply from the PowerDNS under test in `test_infrastructure.py` (`PdnsPublicConfigTests._wait_seconds`): a 3 s `recv`
timed out at load 14-23 (`test_unusual_query_types_are_refused...`, 2026-11-03) while the server was only slow. Hypothesis's 200 ms
per-example deadline failed `test_check_async_lock_scope.py` the same way (251 ms, then 0.92 ms on the retry); `tools/conftest.py`
turns the deadline off for the tools tests (two files already did, per test), the properties are still checked in full.

## 8. Daemon test directories run as several sessions

`pytest daemons/hams_simulated_bots/tests` was one process of 947 tests, 540 s serial (dev box, 2026-11-03; no pytest-xdist in that
daemon's venv, and adding a dependency needs approval): 25 tests of 10-50 s each, real aiortc connections whose time is waiting
(`SFU_OPENING_OFFER_WAIT_S`, ICE, a four-minute over transcribed in chunks), so the machine was idle for most of it. The directory
is now `daemon_shards` in `tools/lint_test_durations.json`: `lint_test_durations.daemon_shards` splits its files into 4 sessions
balanced by recorded seconds per file, run by `run_linters.py` as the steps `pytest daemons/hams_simulated_bots/tests[NofM]`
(`--only "pytest daemons/hams_simulated_bots/tests"` still selects all four). Measured: the four sessions run side by side,
each passing (246 + 448 + 153 + 100 = 947 passed, 13 skipped, the same counts as the serial run), **179 s wall against 540 s**
(load 4 to 17 from other agents; slowest session 179 s, the others 106-151 s; `test_sfu_offers.py` alone is about 150 s). The
sessions do not share state: every server binds port 0 except `test_main.py` (3000) and `test_observer.py` (3001), which are in
different files and different ports. `HAMS_LINT_DAEMON_SHARDS=0` restores the single session. Other daemon directories are
listed in the table only when measured; `lint_test_durations.py refresh` keeps that section.

## 9. The Odoo mypy plugin tests

`tools/test_odoo_mypy_plugin.py` (13 tests, 10 of them in one class, each a cold mypy run with the Odoo plugin: 22-61 s) is skipped unless
the sibling repository is found, so the duration table was measured with it skipped (0.01 s) and the balanced chunk that held it
took **640-714 s on a lint runner** against 100-200 s for the others. Run alone in the real hams_open checkout (dev box, load 9-26):
474 s in one process, **108 s** with the tests spread over four workers by test (`pytest -n 4 --dist load`, load 4-5). The tests are
independent (each makes its own scratch directory and mypy cache). `run_linters.py` now gives the file a step of its own,
`pytest tools/[mypy-plugin]` (workers only when the interpreter has pytest-xdist), and leaves it out of the chunks;
`HAMS_LINT_MYPY_PLUGIN_SPLIT=0` puts it back. Its entry is removed from `lint_test_durations.json`. Pinned by `MypyPluginStepTest`.

The first full run of the split on bruce-yoga (8 cores, other steps running) failed 2 to 8 of the 13 tests with `subprocess.TimeoutExpired`:
the tests' fixed 120 s limit per mypy run is too short with four workers beside the other steps (22-61 s alone). The limit now follows the
load (`_mypy_timeout`, the same rule as section 7). That run's step was 330-336 s on the runner against 536-640 s for the chunk that held the file before.

## 10. Paired full runs, before and after the two lint-speed passes (2026-11-03)

Method. Full `run_linters.py` (no `--changed`, every step) of hams_open at 14f307be with hams_com at ef0660b67 as the sibling, run on the
machine itself over ssh (`--remote local` inside its mirror, `nice -n 19`), one run at a time, the dev box only syncing files.
**Before** = hams_shared at 5ab8b6e (PR #339, before the first pass), with the Cargo directory restored to its old name
`~/.cache/hams-cargo-target-lint` (warm, as the old code used it). **After** = hams_shared at c29b543 (second pass #344-#349), whose first
run adopts that directory (`lint cache: adopted the old shared directory ...`, seen on bruce-yoga twice and on mac1-vm).

bruce-yoga (8 cores, shared with another agent's relay tests; `uptime` 1-minute load before to after each run):

| Run | Wall | Sum of step times | Load | check_cargo_clippy | Slowest step |
|---|---|---|---|---|---|
| old 1 | 600 s | 1,478 s | 0.4 to 1.8 | 109 s | pytest tools chunk 536 s (holds test_odoo_mypy_plugin.py) |
| old 2 | **2,585 s** | 3,749 s | 7.4 to 2.8 | **2,365 s** (the 961 s-class stale-output scan) | clippy |
| new 1 (adopts old directory) | 396 s | 1,517 s | 1.8 to 7.4 | 52 s | mypy-plugin step 335 s |
| new 2 (adopts again) | 459 s | 1,721 s | 2.8 to 7.8 | 73 s | check_untyped_utility_files 445 s |
| new 3 (steady state) | **393 s** | 1,487 s | 6.1 to 6.3 | 44 s | mypy-plugin step 330 s |

Ratios: wall 600 to 393-459 s against the faster old run, 2,585 to 393-459 s against the slower one (old time depends on how many build scripts
the old scan finds; the new scan does not). Clippy 109-2,365 s to 44-73 s. Caveats: load on this runner is another agent's and varies
between runs; the sums are inflated when steps overlap on 8 cores. In this direct mode `pytest tools/[host-bound]`, 8 tests of
`test_run_linters_changed_selection.py` and `test_sweep_orphan_worktrees.py` fail in every run, old and new (the mirror is not a git
checkout; the normal `--remote` path keeps those steps on the dev box), so `rc=1` is the same in all five; the new runs also show 2-8
`test_odoo_mypy_plugin.py` timeouts at 120 s with four workers, fixed in #352 (limit follows the load) and verified alone on the runner
(13 passed, 124 s). Not measured: a `--changed` run on a runner (the logs of the real `--remote auto` path are in section 3 and the PR descriptions).

mac1-vm (10 cores, `uptime` 0.3 to 5.4), cross-check of the first-run adoption only: the old directory was adopted and used (3.6 GB), then
**cleared at once because the disk had 5 GB free against the 10 GB floor** (`/` is 94 % full: 89 of 99 GB used), so clippy was cold. Full run 206 s wall, 601 s
of step times. The VM cannot hold a warm Cargo directory until about 10 GB is freed on it (not done: it hosts the production FCC sync).

The isolated venv of `hams_simulated_bots`' pytest step is cached the same way (`~/.cache/hams-lint-venvs/`, not budgeted): `LINT_DAEMON_VENVS.md`.
