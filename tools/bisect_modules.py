#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Minimal module set that still reproduces named failing tests (`test.py --bisect-modules`, ADR 0118).

A whole-system run installs dozens of modules in one Odoo and a test can fail only because of another module in
that database. Finding which one was done by hand: the failing set, then half of it, a quarter, and so on. This
module does it by a binary search for the modules that are needed: it reruns `test.py` on smaller `-u` sets, through the ordinary
slot queue, one or two runs at a time, and keeps the smallest set that still fails the named tests.

The modules the test tags name are never removed (the test needs its own module); the others are the candidates.
Every run uses `--raw-failures`, so a known-red or flaky test is not hidden or rerun, and reads `test_results.json`.
The reference is the "Minimal module sets" section of tools/README.md.
"""

import concurrent.futures
import glob
import json
import os
import subprocess
import sys
import time

DEFAULT_MAX_RUNS = 40
MAX_JOBS = 2
RESULTS_GLOB = "test_results*.json"


class BisectError(RuntimeError):
    """The bisection cannot start or cannot be trusted (it names the cause)."""


# [@ANCHOR: bisect_modules:find_needed]
def find_needed(items, reproduces, jobs=1, max_runs=DEFAULT_MAX_RUNS, on_step=None):
    """A minimal sublist of `items` for which `reproduces(sublist)` is True, by repeated binary search for a prefix.

    Assumes adding a module never hides the failure. Round by round: with the modules found so far (`needed`), find
    the shortest prefix of the rest that still reproduces (a binary search, or a three-way one with `jobs` 2); its
    last module is needed, so it joins `needed` and the prefix before it is searched next, until `needed` alone
    reproduces. A failure that needs k modules costs about k times log2(n) runs (the manual halving, repeated).
    `items`: ordered candidates; all of them together must reproduce (the caller checked). `reproduces(sublist)`: True when
    every named test still fails with those modules (plus the fixed ones); called from up to `jobs` threads, never twice
    for the same sublist. `max_runs`: stop after that many calls. `on_step(sublist, result)`: called after each run.
    Returns `(needed, complete, runs)`: `needed` in the order of `items`; `complete` False when `max_runs` stopped the search
    (then `needed` plus the unsearched prefix, which is returned as part of it, still reproduces)."""
    cache, count = {}, [0]
    stopped = [False]

    def evaluate(candidates):
        todo = []
        for candidate in candidates:
            if tuple(candidate) not in cache and candidate not in todo:
                todo.append(candidate)
        room = max_runs - count[0]
        if len(todo) > room:
            stopped[0] = True
            todo = todo[:max(room, 0)]
        if todo:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
                for candidate, result in zip(todo, pool.map(reproduces, todo)):
                    cache[tuple(candidate)] = result
                    count[0] += 1
                    if on_step:
                        on_step(candidate, result)
        return [cache.get(tuple(c)) for c in candidates]

    needed, pool = [], list(items)
    while True:
        if evaluate([needed])[0] or stopped[0]:
            break
        if not pool:  # needed alone did not reproduce although the whole set did: the failure is not deterministic
            stopped[0] = True
            break
        low, high = 0, len(pool)  # needed + pool[:low] does not reproduce (checked); pool[:high] does
        while high - low > 1 and not stopped[0]:
            if jobs >= 2 and high - low > 2:
                third = (high - low) / 3.0
                probes = sorted({low + max(1, int(third)), low + max(1, int(2 * third))} - {high})
            else:
                probes = [low + (high - low) // 2]
            results = evaluate([needed + pool[:p] for p in probes])
            for probe, result in zip(probes, results):
                if result:
                    high = probe
                    break
                if result is False:
                    low = probe
        if stopped[0]:
            needed = pool[:high] + needed
            break
        needed = [pool[high - 1]] + needed
        pool = pool[:high - 1]
    ordered = [m for m in items if m in set(needed)]
    return ordered, not stopped[0], count[0]


# [@ANCHOR: bisect_modules:read_failed_tests]
def read_failed_tests(log_directory):
    """The failing test ids a finished run recorded: `new_failures` of every `test_results*.json` in `log_directory`
    (a run in slot K writes `test_results.slot-K.json`). Returns None when there is no such file (the run passed, or it
    died before a verdict: the caller decides which from the exit status). Raises `BisectError` for an unreadable file."""
    found = None
    for path in sorted(glob.glob(os.path.join(log_directory, RESULTS_GLOB))):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            ids = list(data["new_failures"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise BisectError("%s: cannot read the run's results: %s" % (path, error)) from error
        found = (found or []) + ids
    return found


# [@ANCHOR: bisect_modules:child_command]
def child_command(test_py, modules, test_tags, log_directory, extra=()):
    """The `test.py` command line for one trial: `-u <modules> --test-tags <tags> --raw-failures --wait --solo -l <dir>`,
    then `extra`. Modules are comma separated in the given order. Pure."""
    return [
        sys.executable, test_py, "-u", ",".join(modules), "--test-tags", test_tags, "--raw-failures", "--wait", "--solo",
        "-l", log_directory, *extra,
    ]


class Trial:
    """One `test.py` run of a bisection: what was run and what came of it (a plain record for the JSON report)."""

    def __init__(self, modules, exit_status, failed, seconds, log_directory):
        self.modules = list(modules)
        self.exit_status = exit_status
        self.failed = failed
        self.seconds = seconds
        self.log_directory = log_directory

    def as_dict(self, reproduced):
        return {
            "modules": self.modules, "exit_status": self.exit_status, "failed": self.failed,
            "reproduced": reproduced, "seconds": round(self.seconds, 1), "log_directory": self.log_directory,
        }


# [@ANCHOR: bisect_modules:Bisector]
class Bisector:
    """Runs the trials of one bisection and decides which reproduce the failure.

    `test_py`: path of test.py. `fixed`: modules always installed (the ones the tags name). `test_tags`: the Odoo tag
    spec. `work_directory`: parent of each trial's log directory (created mode 0777: the run may be another account).
    `targets`: ids that must fail for a trial to count; None until `start` has run the full set. `runner`: how a command
    is executed, `runner(command) -> exit status` (default `subprocess.call`; tests replace it). Thread safe: trials may run
    from two threads, each in its own log directory; `trials` is appended under a lock."""

    def __init__(self, test_py, fixed, test_tags, work_directory, targets=None, runner=None, extra=()):
        import threading

        self.test_py = test_py
        self.fixed = list(fixed)
        self.test_tags = test_tags
        self.work_directory = work_directory
        self.targets = set(targets) if targets else None
        self.runner = runner or (lambda command: subprocess.call(command))
        self.extra = tuple(extra)
        self.trials = []
        self._lock = threading.Lock()

    # [@ANCHOR: bisect_modules:Bisector_run_trial]
    def run_trial(self, extra_modules):
        """Runs `test.py` on `fixed + extra_modules` and returns the `Trial`."""
        with self._lock:
            number = len(self.trials) + 1
            self.trials.append(None)
        directory = os.path.join(self.work_directory, "trial_%03d" % number)
        os.makedirs(directory, exist_ok=True)
        os.chmod(directory, 0o777)
        modules = self.fixed + [m for m in extra_modules if m not in self.fixed]
        started = time.monotonic()
        status = self.runner(child_command(self.test_py, modules, self.test_tags, directory, self.extra))
        trial = Trial(modules, status, read_failed_tests(directory), time.monotonic() - started, directory)
        with self._lock:
            self.trials[number - 1] = trial
        return trial

    # [@ANCHOR: bisect_modules:Bisector_reproduces]
    def reproduces(self, extra_modules):
        """True when every target test failed in a trial on `fixed + extra_modules`. A trial that died without a
        verdict (no results file but a non-zero status) is not a reproduction."""
        trial = self.run_trial(extra_modules)
        return self.targets is not None and bool(self.targets) and self.targets <= set(trial.failed or [])


# [@ANCHOR: bisect_modules:bisect]
def bisect(bisector, candidates, jobs=1, max_runs=DEFAULT_MAX_RUNS, out=print):
    """Bisects `candidates` with `bisector`; returns the report dictionary (also what `write_report` stores).

    First runs the full set: no failure there means "does not reproduce" (`status` `not_reproduced`, no minimal set).
    The failing tests of that run become the targets. Then `find_needed` reduces `candidates`. `status` is `minimal`,
    `incomplete` (max_runs reached; the set is the smallest found) or `not_reproduced`. Raises `BisectError` when the
    full run has no verdict at all (it died, so nothing can be concluded)."""
    full = bisector.run_trial(candidates)
    out("bisect: full set (%d candidate modules): exit %s, failed %s" % (len(candidates), full.exit_status, full.failed))
    if full.failed is None and full.exit_status != 0:
        raise BisectError("the full run ended with status %s and wrote no results: no verdict to bisect" % full.exit_status)
    report = {"version": 1, "fixed": bisector.fixed, "candidates": list(candidates), "test_tags": bisector.test_tags}
    if not full.failed:
        report.update(status="not_reproduced", targets=[], minimal=None, runs=1, complete=True)
        report["trials"] = [t.as_dict(False) for t in bisector.trials if t]
        return report
    bisector.targets = set(full.failed)

    def note(sublist, result):
        out("bisect: %d candidate module(s) %s -> %s" % (len(sublist), "reproduce" if result else "pass", sorted(sublist)))

    minimal, complete, runs = find_needed(list(candidates), bisector.reproduces, jobs, max_runs, note)
    report.update(
        status="minimal" if complete else "incomplete", targets=sorted(bisector.targets), minimal=bisector.fixed + minimal,
        needed=minimal, runs=runs + 1, complete=complete,
    )
    report["trials"] = [t.as_dict(bool(bisector.targets <= set(t.failed or []))) for t in bisector.trials if t]
    return report


# [@ANCHOR: bisect_modules:write_report]
def write_report(path, report):
    """Writes the report as JSON, atomically; the directory is created. Raises `OSError` when it cannot."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temporary = "%s.tmp%d" % (path, os.getpid())
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


# [@ANCHOR: bisect_modules:run]
def run(args, test_py, tag_modules, runner=None, out=print, slots=1):
    """`test.py --bisect-modules`: returns the exit status (0 a minimal set was found, 1 the failure does not
    reproduce or the search was incomplete, 2 bad arguments, 75 the full run had no verdict).

    `args` is the parsed `test.py` command line (`module` is the `-u` list, `test_tags`, `log_directory`, `bisect_jobs`,
    `bisect_max_runs`, `bisect_report`); `tag_modules` the modules the tags name (fixed); `slots` the machine's slot
    count (jobs default to `min(2, slots)`). Runs at nice 10. Prints the minimal set and the report path."""
    modules = [m.strip() for m in (args.module or "").split(",") if m.strip()]
    fixed = [m for m in modules if m in set(tag_modules)] + [m for m in tag_modules if m not in modules]
    candidates = [m for m in modules if m not in fixed]
    if not candidates:
        out("bisect: -u names no module beyond those the tags name: nothing to reduce.")
        return 2
    jobs = args.bisect_jobs or min(MAX_JOBS, max(1, slots))
    if not 1 <= jobs <= MAX_JOBS:
        out("bisect: --bisect-jobs must be 1 or 2.")
        return 2
    try:
        os.nice(10)
    except OSError:
        pass
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = os.path.abspath(os.path.expanduser(args.log_directory))
    work = os.path.join(base, "bisect_" + stamp)
    report_path = args.bisect_report or os.path.join(base, "bisect_%s.json" % stamp)
    bisector = Bisector(test_py, fixed, args.test_tags, work, runner=runner)
    try:
        report = bisect(bisector, candidates, jobs, args.bisect_max_runs or DEFAULT_MAX_RUNS, out)
    except BisectError as error:
        out("bisect: %s" % error)
        return 75
    write_report(report_path, report)
    if report["status"] == "not_reproduced":
        out("bisect: the failure does not reproduce with the full set; report: %s" % report_path)
        return 1
    out("bisect: %s set (%d runs): -u %s" % (report["status"], report["runs"], ",".join(report["minimal"])))
    out("bisect: tests that still fail: %s" % ", ".join(report["targets"]))
    out("bisect: report: %s" % report_path)
    return 0 if report["complete"] else 1
