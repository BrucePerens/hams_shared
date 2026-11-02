# Lint step analysis: duplicate and overlapping work in `run_linters.py`

Written 2026-11-02 for the "lint runs thrash the dev box" investigation. Numbers come from the step list
(`python3 tools/run_linters.py --list-json`, 148 steps from a `hams_com` run), from the per-step timings that
`lint_exec.Cache` stores in `~/.cache/hams-lint/*.json` (1,376 step names, up to 323 samples each) and from the
logs under `~/workspace/tmp/linttmp`. Timings are medians of runs made under very different load (see 2): read them
as relative sizes, not as budgets.

## 1. Summary

| Finding | Evidence | Consequence |
|---|---|---|
| The reported duplicate (`pytest daemons` covering `relay_ca`, `hams_simulated_bots/tests`, `dx_firehose`, `gdpr_csv_export`) does not exist. | `pytest daemons` is built from `daemons/test_*.py` only (`files_here` is filtered to one directory). `tools/lint_step_inventory.py` collected every pytest step: 57 steps, 7,551 test ids, 298 test files, **0 ids and 0 files collected by two steps**. | Nothing to delete. The failed run looked duplicated because ~57 pytest processes ran. |
| Python tests are 86% of the work. | Sum of median step times 3,253 s: pytest steps 2,830 s (daemons 2,063, tools 675, ingest 92), everything else 423 s. | Combining pays only where it removes pytest work; checker steps are small. |
| Contention, not the step list, is the dominant cost. | The same 26 steps took 1,034 s of step time (`l2.log`, wall 514 s) in one run and 3,384 s (`l4.log`, wall 1,651 s) in another: 3.3 times, no code change. `check_burn_list` 1,647 s against a 139 s median; `pytest daemons` 390 s against 62-102 s. | A run lock (one heavy run per machine) and a load gate come first; see `lint_lock.py`. |
| One pytest session over several daemon directories is not possible. | Collecting the system-python daemon directories in one session gives 13 collection errors (`module 'main' has no attribute ...`: every daemon has its own `main.py` imported by bare name). Two directories need their own `.venv` interpreter. | Per-daemon processes stay. Only directories of one daemon that share interpreter and path are merged (`hams_local_relay`, `hams_local_relay/tests`, `hams_local_relay/tools`: 25 + 3 + 110 = 138 ids in one session, identical). |
| `pytest-xdist` is not installed (pytest 9.1.1, pytest-timeout 2.4.0 only). | `import xdist` fails. | Not added: a new dependency needs Bruce's approval and a pinned hash (AGENTS.md, supply-chain rule). It is the one change that would shorten `relay_ca` (262-710 s in a single process); listed under "decisions needed". |
| Five whole-tree Python/Rust scanners run once per repository, as ten processes. | `check_async_lock_scope`, `check_fake_network_listener`, `check_int_float_overflow`, `check_float_isfinite`, `check_window_fetch_reassignment`, each `[hams_com]` and `[hams_open]`; each takes one root. | One process per checker over both roots (10 to 5 processes); same findings. |
| Two of those scanners parse the same files. | `check_float_isfinite` and `check_int_float_overflow` both `ast.parse` every `.py` of both repositories (18 + 16 s medians per repository). | A shared parse would save about half of 70 s; not done here (needs a refactor of both checkers), recorded as follow-up. |
| Cargo: nothing to share that is not shared already. | `check_cargo_clippy` runs `cargo clippy` once per top-level crate (workspace members are covered by their parent), with no feature variants; `check_cargo_fmt`, `check_cargo_deny`, `check_rust_function_test_anchors` have the `cargo` lock and one target directory (`~/.cache/hams-cargo-target-lint`) set in `_finalise_steps`. There is **no** `cargo test` step in the runner (Rust tests run in CI and by hand). | No change. The brief's `--features hamlib-oracle` clippy run is the agent's own pre-merge command, not a runner step. |
| No node test step exists in the runner. | Console JavaScript tests run under Odoo's hoot (`check_hoot_runner_coverage` only verifies they are registered); `eslint` and `check_js_syntax` are the only node steps. | Nothing to merge. |
| `--changed` already restricts most steps. | `STEP_META` suffix filters and per-step `roots`. Whole-tree by design (documented in the code): `verify_anchors`, `check_dependency_cycles`, `check_self_writeable_field_tests`, the `check_*` repo-wide scanners, `check_claims_freshness`, `check_doc_images`, `check_scss_math_functions`. | Left as they are: each is a property of the whole repository. |

## 2. Where the time goes (medians, seconds; "best" is the fastest sample ever recorded)

| Step group | Steps | Median sum | Best sum | Notes |
|---|---|---|---|---|
| `pytest daemons/*` (one process per daemon directory) | 52 | 2,063 | 630 | `relay_ca` 710 (best 262), `hams_simulated_bots/tests` 585, `pytest daemons` 102, `ham_dx_daemon` 61, `dx_firehose` 56 |
| `pytest tools/[1-4of4]` | 4 | 675 | 244 | already four size-balanced processes |
| `pytest ingest/` | 1 | 92 | 73 | hams_com only |
| `check_burn_list` | 1 | 139 | 1 | one per target when scoped, one process when widened |
| `eslint` | 1 | 65 | 0 | |
| `check_js_syntax` | 1 | 40 | 1 | |
| `check_float_isfinite` / `check_int_float_overflow` | 2 + 2 | 36 / 29 | 1 / 1 | per repository |
| `verify_anchors` | 1 | 21 | 1 | always whole-tree |
| all other steps | ~80 | < 10 each | | |

## 3. Step inventory

`python3 tools/lint_step_inventory.py table STEPS_JSON [COLLECTED_JSON]` prints one row per step (what it runs,
its lock, how many tests it collects). Steps with a lock: `cargo` (`check_rust_function_test_anchors`,
`check_cargo_deny`, `check_cargo_clippy`, `check_cargo_fmt`).

## 4. Coverage-preserving proof

```
python3 tools/run_linters.py --list-json > before_steps.json
python3 tools/lint_step_inventory.py collect before_steps.json before.json --tree "$PWD"
# ... change the step list ...
python3 tools/run_linters.py --list-json > after_steps.json
python3 tools/lint_step_inventory.py collect after_steps.json after.json --tree "$PWD"
python3 tools/lint_step_inventory.py compare before.json after.json    # exit 0: same tests, each once
python3 tools/lint_step_inventory.py duplicates after.json             # exit 0: no id twice
```

`collect` runs each pytest step's own interpreter, cwd and environment with `--collect-only`, so a test that a
step would not have run is not counted. Ids are written relative to `--tree`, so two worktrees compare.

Baseline before any change (hams_com `34f6a2487`, hams_shared `f29d5e3`): 57 steps, 7,551 test ids, 0 duplicates.

## 5. Changes made from this analysis

| Change | Before | After | Proof |
|---|---|---|---|
| hams_local_relay root, `tests/` and `tools/` | 3 pytest processes | 1 step `pytest daemons/hams_local_relay` (old names still work with `--only`) | 138 ids = 25 + 3 + 110, run together: 138 passed in 244 s (the three separately: 392 s) |
| Repository-pair scanners (`check_async_lock_scope`, `check_fake_network_listener`, `check_int_float_overflow`, `check_float_isfinite`, `check_window_fetch_reassignment`) | 10 steps (one per repository) | 5 steps over both repositories; the checkers take several roots | `test_run_linters_changed_selection.py`: a change in hams_com or hams_open selects each |
| Whole-run coverage | 57 pytest steps, 7,551 ids | 55 pytest steps, 7,586 ids (+35 = the new test files of this series) | `lint_step_inventory.py compare before after --allow-new ...`: IDENTICAL, no id lost, none collected twice |

The merge rule (in `run_linters.py`, daemon section): test directories of one daemon that run under the same
interpreter and hold no two test files of the same name are one session. Different daemons are never merged
(section 1). A merged step keeps the old step names as `Step.aliases`, which `--only` accepts, and
`--changed` selects the merged step when any file of the daemon changes.

## 6. Machines (surveyed read-only 2026-11-02; `lint_remote.py probe` repeats it)

| Machine | OS, cores | Tools found | Lint runner? |
|---|---|---|---|
| devbox | Debian, 16 | everything | yes (local) |
| mac1-vm (Lima `hams-test-debian` on mac1) | Debian 13 arm64, 10 | cargo, node, chromium, flake8, odoo, rsync; no pytest | yes: lowest priority (shared with Bruce's Mac and the FCC sync) |
| bruce-yoga | Debian 13 x86-64, 8 | python3 3.13, google-chrome, git; no pytest, flake8, node, cargo, rsync | yes (stdlib-only checkers now; more after `lint_remote.py provision`) |
| mac1 (the Mac) | macOS arm64, 12 | system python 3.9, no cargo or node | no |
| bruce-yoga-win, Bruce_Thinkpad | Windows | PowerShell | no (Windows builds only) |
| hams1, omnia | production, router | | never |

Real end-to-end run (2026-11-02): `run_linters.py --remote bruce-yoga --only check_dependency_cycles` from a
hams_com worktree: first sync 875 MB (full tree) in 62 s wall, a repeat with two steps 3.8 s wall (incremental).

## 7. Decisions needed from Bruce

* Installing sudo-level packages on bruce-yoga and mac1-vm (nodejs, cargo, clippy, rustfmt, chromium, odoo, pytest) so
  `--remote auto` can take the Python-test and Rust steps; `lint_remote.py provision` lists them and does the user-level pip part.
* `pytest-xdist` (or a hand-rolled file split of `relay_ca`) to shorten the longest pytest steps. Needs a pinned,
  hash-checked dependency.
* Whether the two float scanners should share one parse (a refactor of two checkers).
