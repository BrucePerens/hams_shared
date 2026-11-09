# ADR 0118: Known-Red Baseline, One Flake Rerun, and an Automatic Whole-System Canary

## Status
Accepted (Bruce, 2026-11-08: the test infrastructure should stop depending on agents hand-debugging whole-system failures).

## Scope
`tools/test.py` and the files it uses: `tools/known_red.py` (the baseline and the flake rerun), `tools/bisect_modules.py`
(minimal module set), `tools/gate_snapshot.py` (the whole-system gate tree) and `tools/gate_canary.py` (the nightly run).
The reference is the "Known-red baseline and flakes" section of `tools/README.md` and the sections added by the same series.

## Context
One whole-system run (one Odoo, 66 modules, about 3700 tests, 75 minutes) is the only run that sees interactions between
modules. A handful of its tests failed for reasons nobody had yet fixed, and any one of them turned the run red. Each
red run was then investigated by hand by an agent: halving a list of 33 modules four times to find which one makes a test
fail, and rebuilding the gate tree by hand. The gate shards failed three times in one night for stale inputs (a checkout
behind `origin/main`, a `libcloudflared.so` copied over with an old timestamp). A person cannot tell from a red run whether
the failure is new, and a flaky test costs the same hand-debugging as a real one.

## Decision
1. **A known-red baseline.** A tracked file `known_red_baseline.json` lists tests that are red for a known reason, each with
   test id, the commit that first showed it, an owner and a reason. A failing baselined test is reported as "known red, not new"
   and does not turn the run red; any other failure does. A baselined test that passes is reported so its entry is removed.
   Every tolerated test is listed in the run summary: the baseline never hides a test.
2. **One rerun for a failure that is not baselined.** A failed test is rerun once, alone, on a fresh database with the same
   modules installed (same slot and database rules). Passing alone is a flake: counted per test in a small JSON file,
   tolerated, listed. Failing again is a failure. A test whose flake count exceeds a threshold is listed as "FIX ME". A
   setup or class error is never rerun silently: it is reported and is red unless baselined. A failure whose test the log
   does not name, and a systemic number of failures, are red and not rerun.
3. **Automatic minimal module sets.** `test.py --bisect-modules` takes a failing module set and test tags and halves the set
   until it finds a minimal one that still reproduces the named failures, through the ordinary slot queue.
4. **A gate tree that refuses stale inputs.** `gate_snapshot.py` builds the whole-system tree for the commits at
   `origin/main` and refuses to start when an input is stale or missing.
5. **A nightly canary.** `gate_canary.py` runs the whole system on a clean snapshot of `origin/main`, writes the outcome in the
   baseline's format and a status file, and bisects a new failure on its own. The systemd units are provided and not enabled.
6. **Where it runs.** On the machine that holds the checkout, at `nice -n 10`, one or two slots unless nothing else runs. The
   tools add no remote execution path.

## Consequences
A red run means something new broke. Old failures are visible, owned, and cannot be forgotten, because each run prints them.
The risk is that the baseline grows and hides real regressions in the listed tests: a baselined test that fails differently
is not noticed (the id is the same). That is why entries carry an owner and a first-seen commit and are listed on every run.
A rerun costs a database rebuild and a module install per failing run; it runs only when a new failure exists.
