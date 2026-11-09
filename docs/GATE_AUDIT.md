# Gate Audit: a Green Gate Is a Claim, Not Proof

Audit of 2026-11-09, started from Bruce's request that the recommendation "treat a green gate as a claim, not proof" become work
(to-do `night_shift_todo/medium/gate-audit-negative-control-tests-for-every-merge-and-deploy-gate-e710d1be.md`, hams_com #3803).
Same-day evidence of gates that could not fail: `gate_canary` green for a shard that never finished; `prmerge.sh` and the merge
queue reading gitlink-only changes and code-to-`.md` renames as markdown-only (no lint); `Worker._lint` returning 0 on a conflicted
tree; `assert_control_absent` passing when the page had no tree; a release-gate `wait_for` returning error text as the awaited value.

## Inventory: every gate that decides pass or fail for a merge or a deploy

"Negative control" means a test that feeds the gate a deliberately broken input and asserts RED. "Before" is what the audit found.

| Gate | Where | Before the audit | Result |
|---|---|---|---|
| Pass-stamp gate (`refusal`, `inherit_stamp`) | `tools/lint_stamp_gate.py`, `tools/test_lint_stamp_gate.py` | Zero-step, empty, garbage, truncated and old stamps already had controls. A `full` stamp with `steps=0` was accepted. | Fixed, hams_shared #562 |
| `prmerge.sh` (diff, markdown-only exemption, stamp, PR state) | `devbox_tools/prmerge.sh`, `devbox_tools/test_prmerge.py` | Failed `git diff`, gitlink-only change and code-to-`.md` rename already refused (hams_com #3676, #3728, #3804). | RED proven |
| Merge queue (`Worker._lint`, `_merge`, docs lane, retarget, wrong repo) | `devbox_tools/merge_queue.py`, `devbox_tools/test_merge_queue.py` | Conflicted tree, rename, gitlink, wrong repo, pushed-after-test, dequeued, bisection already RED. Exit 0 with no, empty or truncated results file, zero steps, a recorded failed step, and a retry that exits 0 yet records a failure stayed green; `gh` unreachable before the merge merged anyway. | Fixed, hams_com #3815 |
| Lint batch verdicts (`read_results`, `collect`) | `tools/lint_batch.py` | Exit 0 with no or empty results file, `returncode: null`, a selected step with no result passed. | Fixed, hams_shared #565 |
| Pass-stamp write (`run_linters.steps_without_result`) | `tools/run_linters.py`, `tools/test_run_linters.py` | A selected step with no result was stamped as passed. | Fixed, hams_shared #PR_NUMBER |
| Stamp inheritance history check (`_history_problems`) | `tools/lint_stamp_gate.py`, `tools/test_lint_stamp_gate.py` | A failed `git archive` read as "history fine". | Fixed, hams_shared #PR_NUMBER |
| `--changed` selection (`changed_files`) | `tools/run_linters.py` | A failed `git diff` became "nothing changed": every dependent step skipped, a stamp still written. | Fixed, hams_shared #571 |
| Known-red baseline (`failure_items`, `classify`, `judge`) | `tools/lint_baseline.py` | One recorded failing test could hide failures pytest counted but did not list, or an INTERNALERROR. | Fixed, hams_shared #573 |
| Remote lint results (`verify_results`) | `tools/lint_remote.py` | Failed safe by accident (an exception re-ran locally). | Hardened, hams_shared #574 |
| Step execution and cache (`lint_exec.run_step`, `Cache.record`, `retry_timed_out_steps`, `lint_lock`) | `tools/lint_exec.py`, `tools/lint_lock.py` | Crash, signal, missing binary, failed rerun, lock timeout all RED already. | RED proven |
| Module scope (`module-not-found`) | `tools/run_linters.py` | A named module that exists nowhere fails the run (existing). | RED proven |
| Nightly canary (`gate_canary`) | `tools/gate_canary.py`, `tools/test_gate_canary.py` | Died, unnamed, cut-short, killed shard and stale rerun file already RED. A shard that exited 0 with no results file, zero shards, and a relay cargo suite with no counted test stayed green. | Fixed, hams_shared #564 |
| Whole-system snapshot (`gate_snapshot`) | `tools/gate_snapshot.py`, `tools/test_gate_snapshot.py` | Stale commit, edited tree, wrong tree, missing artifacts already RED. A tree with no module gave zero shards. | Fixed, hams_shared #572 |
| Odoo batch result collection | `tools/odoo_test_batch.py`, `tools/lint_batch.py` hook | Exit 0 from an empty `-u` list or "no tests ran" output passed; a remote run where no host got a module passed. | Fixed, hams_shared #570 |
| `test.py` verdict (`exit_status_for_run`) | `tools/test.py`, `tools/test_test.py` | Zero tests, no `odoo.tests.result` line with exit 0 already RED. | RED proven |
| `production_readiness_check` | `devbox_tools/production_readiness_check.py` | Empty or truncated ssh output passed four checks; missing counts read as 0; empty manifest or release list passed; HTTP 500 or 502 on the home page was only a WARN. | Fixed, hams_com #3819 |
| Relay release gate step verdicts | `devbox_tools/relay_release_gate.py` | Zero-test runs (`0 passed, 0 failed`, all skipped, Odoo `of 0 tests`) and a page row naming no tests passed. | Fixed, hams_com #3818 |
| Relay gate `wait_for` | `devbox_tools/relay_gate/world.py` | A poll that raises was returned as the awaited value; already fixed, `test_a_poll_that_raises_is_not_the_condition_holding`. | RED proven |
| `deploy_to_production.checkout_state` | `devbox_tools/deploy_to_production.py` | A non-repository directory or a missing `origin/main` gave two equal empty shas (clean, at main); a failed fetch left `origin/main` stale. | Fixed, hams_com #3820 |
| Claims freshness | `tools/check_claims_freshness.py` | A non-git tree printed SUCCESS (empty file list); an unreadable claim file was skipped. | Fixed, hams_shared #569 |
| Burn list entry | `tools/check_burn_list.py` | A nonexistent directory printed "Checked 0 files ... Passed". | Fixed, hams_shared #568 |
| `assert_control_absent` and siblings | `control_inventory_audit/tests/control_inventory_common.py` | Already red on a page with no tree (the 2026-11-09 fix). | RED proven |
| Other `wait_for` helpers (`shack_console`, `cert_login_e2e`, `relay_e2e`) | their test helpers | Raise on timeout, or return False that callers check. | Reviewed, fine |

## Known gaps left open

- About 40 of the 53 `tools/check_*.py` linters exit 0 when run by hand on a nonexistent directory (`check_js_syntax`, `check_owl_templates`,
  `check_shebang`, `check_test_tags`, ...). `run_linters.py` itself cannot hand them one: a named module that exists nowhere fails the run
  (`module-not-found`), and `--changed` derives its modules from existing paths. Only a direct command line is exposed.
- ~~`lint_stamp_gate._history_problems` reported "no problems" when `git archive` of the history failed~~ Fixed, hams_shared #PR_NUMBER: it now lists the tree with `git ls-tree` (no history paths there is the legitimate hams_shared case) and a failed `ls-tree`, `archive` or unpack is a problem naming the cause. Tests: `tools/test_lint_stamp_gate.py::HistoryProblemsTests`.
- `prmerge.sh`: an empty path list reads as "markdown-only", which only a branch with no change at all can produce. Any `.md` change is exempt even
  though `verify_anchors` and the documentation checkers read markdown (the stamp-inheritance list in `lint_stamp_gate.EXEMPT_PREFIXES` is narrower
  on purpose, and the merge exemption is not).
- ~~`run_linters.py` did not assert, before writing a pass stamp, that every selected step produced a result~~ Fixed, hams_shared #PR_NUMBER: `steps_without_result` names them, the run fails (after the known-red baseline, which cannot tolerate it) and no stamp is written. Tests: `tools/test_run_linters.py::StepsWithoutResultTests`.
- `relay_gate` scenario that prints PASS for a row and crashes afterwards keeps that row's PASS.
- `deploy_to_production.upgrade_in_progress` returns False when ssh is unreachable (deliberate: the rsync fails next).

## Checklist: how to write a gate that can fail

A gate is any code whose output someone reads as "safe to merge" or "safe to deploy". Before you call it done:

1. **Name the positive evidence** for a pass (a counted test, a result line with N > 0, a stamp naming the steps, a sha that compares unequal to
   an error). A pass is that evidence being present, never the absence of an error.
2. **Treat every unknown as red.** Empty output, missing file, truncated file, wrong shape (`[]` for `{}`), `None`, a field that is absent,
   `returncode: null`: red. `x or []`, `.get(k, 0)`, `except: pass` and `|| true` on a gate's input are the usual leaks.
3. **Never let two failures compare equal.** Two empty strings from two failed `git rev-parse` calls are "the same commit". Raise on a failed
   subprocess and compare only successful results.
4. **Count the work.** Zero tests, zero files scanned, zero shards, zero modules, zero selected steps is red unless an explicit reason says
   nothing was expected (a markdown-only change). Print the count so a reader sees it.
5. **Do not trust an exit code alone.** Exit 0 with error text, a crashed child that left an old results file, a killed shard, a timeout: read
   the result record and check it belongs to this run (fresh path, matching commit).
6. **Fail closed on a failed fetch, a failed `gh` call, an unreachable host.** If the gate could not look, the answer is "could not tell", which
   blocks.
7. **Check what you are checking.** Right repo, right commit, right tree (a conflicted, dirty or stale tree is red), right branch tip after a
   push that happened during the run.
8. **Rename and submodule diffs**: use `git diff --no-renames --ignore-submodules=none` whenever a file list decides an exemption.
9. **Write the negative-control test with the gate.** One test per broken input: empty result, missing file, crashed child, wrong repo,
   truncated output, conflicted tree, zero tests ran, exit 0 with error text. Each asserts the gate is RED and that the reason names the cause.
   Run it against the unfixed gate first and see it fail.
10. **Add the gate to the inventory above** with the path of its negative-control tests.
