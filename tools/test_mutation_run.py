#!/usr/bin/env python3
# Copyright © Bruce Perens K6BP. All Rights Reserved.
"""Tests for mutation_run.py: result parsing, idempotence, the load stop, and one real run on a throwaway tree (the live file stays byte-identical).

Tests [@ANCHOR: mutation_run:parse_harness_log]
Tests [@ANCHOR: mutation_run:build_result]
Tests [@ANCHOR: mutation_run:fingerprint]
Tests [@ANCHOR: mutation_run:is_fresh]
Tests [@ANCHOR: mutation_run:run_target]
Tests [@ANCHOR: mutation_run:run_worker]
Tests [@ANCHOR: mutation_run:summary_line]
Tests [@ANCHOR: mutation_run:result_path]
Tests [@ANCHOR: mutation_run:main]
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import mutation_run  # noqa: E402
import overnight_common as common  # noqa: E402

heavy = unittest.skipIf(os.environ.get("HAMS_SKIP_HEAVY"), "heavy: runs the mutation harness and ram_dirs.py")
LOG = "9 mutants\nscore 5/9 = 55.6%\nSURVIVED line 2: condition -> True\nSURVIVED line 4: binop Sub->Add\n"


class FakeLoad:
    def __init__(self, reason=None):
        self.reason = reason

    def critical(self):
        return self.reason


class ParseTests(unittest.TestCase):
    def test_parses_counts_and_survivors(self):
        parsed = mutation_run.parse_harness_log(LOG)
        self.assertEqual((parsed["mutants"], parsed["killed"], parsed["run"], parsed["partial"]), (9, 5, 9, False))
        self.assertEqual(parsed["survivors"], [{"line": 2, "operator": "condition -> True"}, {"line": 4, "operator": "binop Sub->Add"}])

    def test_partial_run_is_flagged_and_empty_log_is_zero(self):
        self.assertTrue(mutation_run.parse_harness_log(LOG + "PARTIAL: 9 of 12 mutants run before the budget of 60 s\n")["partial"])
        self.assertEqual(mutation_run.parse_harness_log("")["mutants"], 0)

    def test_result_document_has_file_line_operator_and_score(self):
        target = {"name": "m", "file": "tools/m.py", "root": "/nonexistent", "test": "t"}
        document = mutation_run.build_result(target, mutation_run.parse_harness_log(LOG), "done", 3.0, "/log", True, now=lambda: 1.0)
        self.assertEqual(document["score"], 55.6)
        self.assertEqual(document["survivors"][0], {"file": "tools/m.py", "line": 2, "operator": "condition -> True"})
        self.assertEqual((document["mutants"], document["killed"], document["survived"], document["not_run"]), (9, 5, 2, 0))

    def test_no_mutant_run_has_no_score(self):
        document = mutation_run.build_result({"name": "m", "file": "f", "root": "/x", "test": "t"}, mutation_run.parse_harness_log(""), "error", 0, "", None)
        self.assertIsNone(document["score"])


class FreshnessTests(unittest.TestCase):
    def test_fresh_only_when_done_same_fingerprint_and_young(self):
        current = {"source_sha256": "a", "test": "t", "funcs": []}
        done = dict(current, status="done", finished_epoch=1000.0)
        self.assertTrue(mutation_run.is_fresh(done, current, 7, now=lambda: 1000.0 + 86400))
        self.assertFalse(mutation_run.is_fresh(done, current, 7, now=lambda: 1000.0 + 8 * 86400))
        self.assertFalse(mutation_run.is_fresh(dict(done, source_sha256="b"), current, 7, now=lambda: 1001.0))
        self.assertFalse(mutation_run.is_fresh(dict(done, status="partial"), current, 7, now=lambda: 1001.0))
        self.assertFalse(mutation_run.is_fresh(None, current, 7))


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"HAMS_REPORTS_DIR": os.path.join(self.tmp.name, "rep"), "HAMS_JOBS_DIR": os.path.join(self.tmp.name, "jobs")})
        env.start()
        self.addCleanup(env.stop)
        self.root = os.path.join(self.tmp.name, "tree")
        os.makedirs(os.path.join(self.root, "tools"))
        with open(os.path.join(self.root, "tools", "m.py"), "w") as handle:
            handle.write("def f(a, b):\n    if a > b:\n        return a - b\n    return b - a\n")
        with open(os.path.join(self.root, "tools", "test_m.py"), "w") as handle:
            handle.write("import m\n\ndef test_f():\n    assert m.f(3, 1) == 2\n")
        self.targets = os.path.join(self.tmp.name, "t.toml")
        with open(self.targets, "w") as handle:
            handle.write('[[target]]\nname="m"\nroot="%s"\nfile="tools/m.py"\ncwd="tools"\ntest="python3 -m pytest -q -x test_m.py"\nbudget_minutes=1\nworkers=2\n' % self.root)

    def test_load_hold_runs_nothing_and_exits_4(self):
        code = mutation_run.main([self.targets], load=lambda: FakeLoad("load HOLD: busy"))
        self.assertEqual(code, common.EXIT_NOT_IDLE)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "rep", "mutation", "m.json")))
        self.assertIn("stopped before m", open(os.path.join(self.tmp.name, "rep", "overnight.log")).read())

    def test_dry_run_starts_nothing(self):
        self.assertEqual(mutation_run.main([self.targets, "--dry-run"], load=lambda: FakeLoad()), 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "rep", "mutation", "m.json")))

    def test_missing_target_file_is_a_failed_result_not_a_crash(self):
        os.unlink(os.path.join(self.root, "tools", "m.py"))
        self.assertEqual(mutation_run.main([self.targets], load=lambda: FakeLoad()), 1)
        self.assertEqual(common.read_json(os.path.join(self.tmp.name, "rep", "mutation", "m.json"))["status"], "error")

    def test_bad_targets_file_exits_2(self):
        self.assertEqual(mutation_run.main([os.path.join(self.tmp.name, "none.toml")], load=lambda: FakeLoad()), 2)

    @heavy
    def test_real_run_finds_survivors_keeps_the_live_file_and_a_rerun_is_skipped(self):
        live = os.path.join(self.root, "tools", "m.py")
        before = open(live, "rb").read()
        self.assertEqual(mutation_run.main([self.targets], load=lambda: FakeLoad()), 0)
        document = common.read_json(os.path.join(self.tmp.name, "rep", "mutation", "m.json"))
        self.assertEqual(document["status"], "done")
        self.assertTrue(document["original_unchanged"])
        self.assertGreater(document["survived"], 0)
        self.assertEqual({"file", "line", "operator"}, set(document["survivors"][0]))
        self.assertEqual(open(live, "rb").read(), before)
        status, _ = mutation_run.run_target(common.load_targets(self.targets)[0], os.path.join(self.tmp.name, "rep", "mutation"))
        self.assertEqual(status, "skipped")

    @heavy
    def test_a_failing_baseline_is_reported_not_mutated(self):
        with open(os.path.join(self.root, "tools", "test_m.py"), "w") as handle:
            handle.write("def test_f():\n    assert False\n")
        self.assertEqual(mutation_run.main([self.targets], load=lambda: FakeLoad()), 1)
        self.assertEqual(common.read_json(os.path.join(self.tmp.name, "rep", "mutation", "m.json"))["status"], "baseline_fails")

    def test_summary_line_counts_statuses_and_survivors(self):
        path = os.path.join(self.tmp.name, "r.json")
        common.write_json(path, {"survived": 3})
        self.assertEqual(mutation_run.summary_line([("a", "done", path), ("b", "skipped", path)]), "mutation: 2 target(s): 1 done, 1 skipped; survivors 3")

    def test_result_path_is_slugged(self):
        self.assertEqual(mutation_run.result_path("/o", "a b"), "/o/a-b.json")


if __name__ == "__main__":
    unittest.main()
