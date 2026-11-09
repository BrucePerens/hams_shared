#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for bisect_modules.py (ADR 0118): the delta-debugging search, the trial runner with a fake `test.py`, and the
`--bisect-modules` command line of the real runner.

Tests [@ANCHOR: bisect_modules:find_needed]
Tests [@ANCHOR: bisect_modules:read_failed_tests]
Tests [@ANCHOR: bisect_modules:child_command]
Tests [@ANCHOR: bisect_modules:Bisector]
Tests [@ANCHOR: bisect_modules:bisect]
Tests [@ANCHOR: bisect_modules:write_report]
Tests [@ANCHOR: bisect_modules:run]
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace

import bisect_modules

_HERE = os.path.dirname(os.path.abspath(__file__))
MODULES = ["m%02d" % i for i in range(1, 34)]


class FindNeededTests(unittest.TestCase):
    def search_with(self, oracle, items=MODULES, **kwargs):
        return bisect_modules.find_needed(items, oracle, **kwargs), []

    def search(self, needed, items=MODULES, **kwargs):
        calls = []
        lock = threading.Lock()

        def reproduces(sublist):
            with lock:
                calls.append(tuple(sublist))
            return set(needed) <= set(sublist)

        return bisect_modules.find_needed(items, reproduces, **kwargs), calls

    # Tests [@ANCHOR: bisect_modules:find_needed]
    def test_one_culprit_among_33_is_found_in_few_runs(self):
        (minimal, complete, runs), calls = self.search({"m17"})
        self.assertEqual((minimal, complete), (["m17"], True))
        self.assertLessEqual(runs, 14)
        self.assertEqual(len(calls), len(set(calls)), "a set is never run twice")

    # Tests [@ANCHOR: bisect_modules:find_needed]
    def test_two_modules_needed_together_are_both_kept_and_nothing_else(self):
        for needed in ({"m03", "m30"}, {"m01", "m02"}, {"m16", "m17"}):
            (minimal, complete, _), _ = self.search(needed)
            self.assertEqual((set(minimal), complete), (needed, True), needed)

    # Tests [@ANCHOR: bisect_modules:find_needed]
    def test_a_scattered_set_is_found_exactly_with_one_or_two_jobs_in_a_few_dozen_runs(self):
        needed = {"m02", "m11", "m19", "m33"}
        for jobs in (1, 2):
            (minimal, complete, runs), _ = self.search(needed, jobs=jobs)
            self.assertEqual((set(minimal), complete), (needed, True))
            self.assertLessEqual(runs, 40, jobs)

    # Tests [@ANCHOR: bisect_modules:find_needed]
    def test_a_failure_that_does_not_reproduce_twice_stops_the_search_as_incomplete(self):
        answers = iter([False, True, False, False, False, False, False, False])
        (minimal, complete, _), _ = self.search_with(lambda s: next(answers, False))
        self.assertFalse(complete)

    # Tests [@ANCHOR: bisect_modules:find_needed]
    def test_a_failure_that_needs_no_extra_module_gives_the_empty_set_in_one_run(self):
        (minimal, complete, runs), _ = self.search(set())
        self.assertEqual((minimal, complete, runs), ([], True, 1))

    # Tests [@ANCHOR: bisect_modules:find_needed]
    def test_the_run_budget_stops_the_search_with_the_smallest_set_found(self):
        (minimal, complete, runs), _ = self.search({"m05", "m20", "m31"}, max_runs=4)
        self.assertFalse(complete)
        self.assertLessEqual(runs, 4)
        self.assertTrue({"m05", "m20", "m31"} <= set(minimal))

    # Tests [@ANCHOR: bisect_modules:find_needed]
    def test_an_incomplete_search_still_returns_a_set_that_reproduces(self):
        items = ["a", "b", "c", "d", "e"]
        for needed in ({"a"}, {"e"}, {"b", "d"}, {"a", "e"}, {"c"}):
            for jobs in (1, 2):
                for budget in range(1, 12):
                    (minimal, complete, _), _ = self.search(needed, items=items, jobs=jobs, max_runs=budget)
                    self.assertTrue(needed <= set(minimal), (needed, jobs, budget, minimal, complete))

    # Tests [@ANCHOR: bisect_modules:find_needed]
    def test_every_step_is_reported(self):
        steps = []
        bisect_modules.find_needed(MODULES, lambda s: "m09" in s, on_step=lambda s, r: steps.append((len(s), r)))
        self.assertTrue(steps and any(r for _, r in steps))


def fake_runner(needed, fail_ids=("ham_club:TestA.test_x",), die_on=None):
    """A stand-in for `test.py`: fails `fail_ids` and writes test_results.json when every module in `needed` is in `-u`."""
    seen = []

    def runner(command):
        modules = command[command.index("-u") + 1].split(",")
        directory = command[command.index("-l") + 1]
        seen.append(command)
        if die_on and die_on(modules):
            return 3
        if set(needed) <= set(modules):
            with open(os.path.join(directory, "test_results.json"), "w") as handle:
                json.dump({"version": 1, "new_failures": list(fail_ids)}, handle)
            return 1
        return 0

    runner.seen = seen
    return runner


class BisectorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    # Tests [@ANCHOR: bisect_modules:child_command]
    def test_the_child_command_is_a_raw_queued_solo_run_of_the_given_modules(self):
        command = bisect_modules.child_command("/x/test.py", ["a", "b"], "/a:T", "/logs")
        self.assertEqual(command[1:3], ["/x/test.py", "-u"])
        self.assertEqual(command[command.index("-u") + 1], "a,b")
        for flag in ("--raw-failures", "--wait", "--solo"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("--test-tags") + 1], "/a:T")

    # Tests [@ANCHOR: bisect_modules:read_failed_tests]
    def test_results_files_are_read_and_a_missing_or_corrupt_one_is_told_apart(self):
        self.assertIsNone(bisect_modules.read_failed_tests(self.directory.name))
        with open(os.path.join(self.directory.name, "test_results.slot-2.json"), "w") as handle:
            json.dump({"new_failures": ["a:B.c"]}, handle)
        self.assertEqual(bisect_modules.read_failed_tests(self.directory.name), ["a:B.c"])
        with open(os.path.join(self.directory.name, "test_results.json"), "w") as handle:
            handle.write("{")
        with self.assertRaises(bisect_modules.BisectError):
            bisect_modules.read_failed_tests(self.directory.name)

    # Tests [@ANCHOR: bisect_modules:Bisector]
    # Tests [@ANCHOR: bisect_modules:bisect]
    def test_the_whole_search_with_a_fake_runner_finds_the_two_needed_modules(self):
        runner = fake_runner({"ham_club", "m20", "m07"})
        bisector = bisect_modules.Bisector("/x/test.py", ["ham_club"], "/ham_club:TestA", self.directory.name, runner=runner)
        report = bisect_modules.bisect(bisector, MODULES, jobs=2, out=lambda *_: None)
        self.assertEqual(report["status"], "minimal")
        self.assertEqual(sorted(report["needed"]), ["m07", "m20"])
        self.assertEqual(report["minimal"], ["ham_club", "m07", "m20"])
        self.assertEqual(report["targets"], ["ham_club:TestA.test_x"])
        self.assertEqual(report["runs"], len(runner.seen))
        self.assertTrue(all(t["modules"][0] == "ham_club" for t in report["trials"]))
        self.assertTrue(report["trials"][0]["reproduced"])

    # Tests [@ANCHOR: bisect_modules:bisect]
    def test_a_failure_that_does_not_reproduce_with_the_full_set_is_reported_as_such(self):
        bisector = bisect_modules.Bisector("/x", ["ham_club"], "/ham_club:T", self.directory.name, runner=fake_runner({"never"}))
        report = bisect_modules.bisect(bisector, MODULES, out=lambda *_: None)
        self.assertEqual((report["status"], report["minimal"], report["runs"]), ("not_reproduced", None, 1))

    # Tests [@ANCHOR: bisect_modules:bisect]
    def test_a_full_run_that_dies_without_a_verdict_is_an_error_and_a_dying_trial_does_not_reproduce(self):
        bisector = bisect_modules.Bisector("/x", ["ham_club"], "/ham_club:T", self.directory.name, runner=fake_runner({"m01"}, die_on=lambda m: True))
        with self.assertRaises(bisect_modules.BisectError):
            bisect_modules.bisect(bisector, MODULES, out=lambda *_: None)
        runner = fake_runner({"m01"}, die_on=lambda m: len(m) < 3)
        bisector = bisect_modules.Bisector("/x", ["ham_club"], "/ham_club:T", self.directory.name, runner=runner)
        report = bisect_modules.bisect(bisector, MODULES, out=lambda *_: None)
        self.assertIn("m01", report["minimal"])

    # Tests [@ANCHOR: bisect_modules:write_report]
    def test_the_report_is_written_as_json(self):
        path = os.path.join(self.directory.name, "sub", "r.json")
        bisect_modules.write_report(path, {"status": "minimal"})
        with open(path) as handle:
            self.assertEqual(json.load(handle), {"status": "minimal"})


class RunTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def args(self, **over):
        values = dict(module=",".join(["ham_club"] + MODULES), test_tags="/ham_club:TestA", log_directory=self.directory.name,
                      bisect_jobs=None, bisect_max_runs=None, bisect_report=None)
        values.update(over)
        return SimpleNamespace(**values)

    # Tests [@ANCHOR: bisect_modules:run]
    def test_run_prints_the_minimal_set_writes_the_report_and_exits_zero(self):
        lines = []
        status = bisect_modules.run(self.args(), "/x/test.py", ["ham_club"], runner=fake_runner({"ham_club", "m12"}), out=lines.append, slots=2)
        self.assertEqual(status, 0)
        self.assertTrue(any("-u ham_club,m12" in line for line in lines), lines)
        report_path = [line.split("report: ")[1] for line in lines if "report: " in line][0]
        with open(report_path) as handle:
            self.assertEqual(json.load(handle)["needed"], ["m12"])

    # Tests [@ANCHOR: bisect_modules:run]
    def test_run_refuses_nothing_to_reduce_and_a_bad_job_count_and_reports_exit_codes(self):
        out = []
        self.assertEqual(bisect_modules.run(self.args(module="ham_club"), "/x", ["ham_club"], out=out.append), 2)
        self.assertEqual(bisect_modules.run(self.args(bisect_jobs=3), "/x", ["ham_club"], out=out.append), 2)
        self.assertEqual(bisect_modules.run(self.args(), "/x", ["ham_club"], runner=fake_runner({"never"}), out=out.append), 1)
        self.assertEqual(
            bisect_modules.run(self.args(), "/x", ["ham_club"], runner=fake_runner({"m01"}, die_on=lambda m: True), out=out.append), 75
        )
        self.assertEqual(
            bisect_modules.run(self.args(bisect_max_runs=3), "/x", ["ham_club"], runner=fake_runner({"m03", "m30"}), out=out.append), 1
        )

    # Tests [@ANCHOR: bisect_modules:run]
    def test_the_real_runner_command_line_requires_the_failing_set_and_tags(self):
        for argv in (["--bisect-modules"], ["--bisect-modules", "-u", "a,b"], ["--bisect-modules", "--test-tags", "/a:T"]):
            done = subprocess.run([sys.executable, os.path.join(_HERE, "test.py"), *argv], capture_output=True, text=True)
            self.assertEqual(done.returncode, 2, argv)
            self.assertIn("--bisect-modules needs", done.stderr)


if __name__ == "__main__":
    unittest.main()
