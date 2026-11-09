#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for lint_baseline.py, the known-red baseline of the lint run (ADR 0117), and the pieces of
run_linters.py, lint_batch.py and lint_stamp_gate.py that carry it into a verdict and a pass stamp.

The canary test uses a real git repository with an origin and the real combined-tree builder; only the
linter command is a fake script that writes the `--results-json` file.

Tests [@ANCHOR: lint_baseline:BaselineFile]
Tests [@ANCHOR: lint_baseline:enabled]
Tests [@ANCHOR: lint_baseline:baseline_dir]
Tests [@ANCHOR: lint_baseline:baseline_path]
Tests [@ANCHOR: lint_baseline:normalize_message]
Tests [@ANCHOR: lint_baseline:failure_items]
Tests [@ANCHOR: lint_baseline:items_from_failures]
Tests [@ANCHOR: lint_baseline:read_baseline]
Tests [@ANCHOR: lint_baseline:write_baseline]
Tests [@ANCHOR: lint_baseline:classify]
Tests [@ANCHOR: lint_baseline:judge]
Tests [@ANCHOR: lint_baseline:report_lines]
Tests [@ANCHOR: lint_baseline:stamp_field]
Tests [@ANCHOR: lint_baseline:refresh]
Tests [@ANCHOR: lint_baseline:show_lines]
Tests [@ANCHOR: lint_batch:read_known_red]
Tests [@ANCHOR: lint_stamp_gate:known_red_note]
Tests [@ANCHOR: run_linters:baseline_command]
"""

import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import lint_baseline as lb  # noqa: E402
import lint_batch  # noqa: E402
import lint_stamp_gate  # noqa: E402
import run_linters  # noqa: E402
import test_batch_queue as helpers  # noqa: E402

PYTEST_OUT = """..F.
=========================== short test summary info ============================
FAILED tools/test_a.py::TestA::test_one - AssertionError: 3 != 4 at /tmp/pytest-of-x/pytest-12/a.py:44
ERROR tools/test_b.py - ImportError: no module named zzz
2 failed, 90 passed in 12.30s
"""


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"XDG_CACHE_HOME": os.path.join(self.tmp.name, "cache"),
                    "HAMS_BATCH_TREE_DIR": os.path.join(self.tmp.name, "trees")}


class ItemTests(Env):
    def test_switch_and_paths(self):
        self.assertTrue(lb.enabled({}))
        self.assertFalse(lb.enabled({"HAMS_LINT_BASELINE": "off"}))
        self.assertFalse(lb.enabled({"HAMS_LINT_BASELINE": "0"}))
        self.assertEqual(lb.baseline_dir(self.env), os.path.join(self.env["XDG_CACHE_HOME"], "hams-lint", "baseline"))
        self.assertTrue(lb.baseline_path("hams_com", self.env).endswith("baseline/hams_com.json"))

    def test_normalize_removes_what_changes_between_runs_and_keeps_numbers(self):
        a = lb.normalize_message("3 != 4 at /tmp/x/a.py:44 in 1.5s 0xdeadbeef run deadbeef01")
        b = lb.normalize_message("3 != 4 at /var/y/a.py:90 in 20ms 0xcafe0001 run cafecafe99")
        self.assertEqual(a, b)
        self.assertNotEqual(lb.normalize_message("3 != 4"), lb.normalize_message("3 != 5"))

    def test_pytest_lines_give_one_item_per_test_without_the_shard(self):
        items = lb.failure_items("pytest tools/[2of4]", PYTEST_OUT)
        self.assertEqual(set(items), {"pytest tools/::tools/test_a.py::TestA::test_one", "pytest tools/::tools/test_b.py"})
        other_shard = lb.failure_items("pytest tools/[3of4]", PYTEST_OUT)
        self.assertEqual(items, other_shard)

    def test_unittest_and_cargo_lines(self):
        unit = "FAIL: test_x (pkg.mod.Case.test_x)\n----\nTraceback\nAssertionError: nope\n\nRan 3 tests\n"
        got = lb.failure_items("unit", "ERROR: test_y (pkg.mod.Case)\n---\nValueError: bad\n=====\n" + unit)
        self.assertIn("unit::pkg.mod.Case.test_y", got)
        self.assertIn("unit::pkg.mod.Case.test_x", got)
        self.assertEqual(got["unit::pkg.mod.Case.test_y"]["message"], "ValueError: bad")
        cargo = lb.failure_items("cargo test", "test a::b::c ... FAILED\ntest a::d ... ok\n")
        self.assertEqual(list(cargo), ["cargo test::a::b::c"])

    def test_step_without_tests_is_one_item_whose_signature_follows_the_findings(self):
        out1 = "x.py:1: E1 bad\nx.py:2: E2 worse\n....\n"
        out2 = "x.py:2: E2 worse\nx.py:1: E1 bad\n\n"
        out3 = out1 + "y.py:9: E3 new\n"
        one, two, three = (lb.failure_items("flake8", o)["flake8"] for o in (out1, out2, out3))
        self.assertEqual(one["signature"], two["signature"])
        self.assertNotEqual(one["signature"], three["signature"])
        merged = lb.items_from_failures({"flake8": out1, "pytest tools/[1of2]": PYTEST_OUT})
        self.assertEqual(len(merged), 3)


class BaselineFileTests(Env):
    def test_write_read_and_since_is_carried_over(self):
        self.assertIsNone(lb.read_baseline("hams_com", self.env))
        items = lb.failure_items("flake8", "x.py:1: E1 bad\n")
        first = lb.write_baseline("hams_com", "aaa111", items, self.env)
        self.assertEqual(first["items"]["flake8"]["since"], "aaa111")
        self.assertEqual(lb.read_baseline("hams_com", self.env)["main"], "aaa111")
        again = lb.write_baseline("hams_com", "bbb222", items, self.env, previous=first)
        self.assertEqual(again["main"], "bbb222")
        self.assertEqual(again["items"]["flake8"]["since"], "aaa111")
        changed = lb.failure_items("flake8", "x.py:1: E1 different\n")
        third = lb.write_baseline("hams_com", "ccc333", changed, self.env, previous=again)
        self.assertEqual(third["items"]["flake8"]["since"], "ccc333")
        with open(lb.baseline_path("hams_com", self.env), "w") as handle:
            handle.write("not json")
        self.assertIsNone(lb.read_baseline("hams_com", self.env))
        self.assertEqual(lb.show_lines("nothing", self.env)[0][:9], "baseline:")

    def test_show_lines_list_items(self):
        lb.write_baseline("hams_com", "aaa111", lb.failure_items("flake8", "x.py:1: bad\n"), self.env)
        text = "\n".join(lb.show_lines("hams_com", self.env))
        self.assertIn("flake8", text)
        self.assertIn("1 known-red", text)


class JudgeTests(Env):
    def doc(self, failed, since="aaa"):
        items = lb.items_from_failures(failed)
        return {"items": {k: dict(v, since=since) for k, v in items.items()}, "main": "aaa"}

    def test_same_failure_is_tolerated_new_and_changed_ones_block(self):
        base = self.doc({"pytest tools/[1of2]": PYTEST_OUT})
        same = lb.items_from_failures({"pytest tools/[2of2]": PYTEST_OUT})
        tolerated, blocking = lb.classify(same, base)
        self.assertEqual(len(tolerated), 2)
        self.assertEqual(blocking, {})
        worse = PYTEST_OUT.replace("3 != 4", "5 != 6") + "FAILED tools/test_c.py::t - boom\n"
        tolerated, blocking = lb.classify(lb.items_from_failures({"pytest tools/": worse}), base)
        self.assertEqual(len(tolerated), 1)
        reasons = sorted(blocking.values())
        self.assertTrue(reasons[0].startswith("changed"))
        self.assertTrue(reasons[1].startswith("new"))
        self.assertEqual(lb.classify(same, None)[0], {})

    def test_since_must_be_in_the_branch_history(self):
        base = self.doc({"flake8": "x.py:1: bad\n"}, since="feedface")
        items = lb.items_from_failures({"flake8": "x.py:1: bad\n"})
        seen = []

        def contains(repo, commit, head):
            seen.append((commit, head))
            return False

        tolerated, blocking = lb.classify(items, base, repo="/r", head="HEAD", contains=contains)
        self.assertEqual(tolerated, {})
        self.assertIn("rebase", blocking["flake8"])
        self.assertEqual(seen, [("feedface", "HEAD")])

    def test_judge_passes_only_when_every_failure_is_tolerated(self):
        work = helpers.make_repo(self.tmp.name)
        main = helpers.git(work, "rev-parse", "HEAD")
        failed = {"flake8": "x.py:1: bad\n"}
        lb.write_baseline("hams_demo", main, lb.items_from_failures(failed), self.env)
        env = dict(self.env)
        verdict = lb.judge(failed, "hams_demo", work, "HEAD", env)
        self.assertTrue(verdict["passes"])
        mixed = dict(failed, check_x="boom\n")
        verdict = lb.judge(mixed, "hams_demo", work, "HEAD", env)
        self.assertFalse(verdict["passes"])
        self.assertEqual(list(verdict["blocking"]), ["check_x"])
        self.assertEqual(list(verdict["tolerated"]), ["flake8"])
        off = lb.judge(failed, "hams_demo", work, "HEAD", dict(env, HAMS_LINT_BASELINE="off"))
        self.assertFalse(off["passes"])
        self.assertFalse(lb.judge({}, "hams_demo", work, "HEAD", env)["passes"])
        self.assertFalse(lb.judge(failed, "no_such_repo", work, "HEAD", env)["passes"])

    def test_report_and_stamp_field_name_what_was_tolerated(self):
        work = helpers.make_repo(self.tmp.name)
        main = helpers.git(work, "rev-parse", "HEAD")
        lb.write_baseline("hams_demo", main, lb.items_from_failures({"flake8": "x.py:1: bad\n"}), self.env)
        verdict = lb.judge({"flake8": "x.py:1: bad\n", "other": "y\n"}, "hams_demo", work, "HEAD", self.env)
        text = "\n".join(lb.report_lines(verdict))
        self.assertIn("KNOWN RED ON MAIN", text)
        self.assertIn("known red: flake8", text)
        self.assertIn("BLOCKS: other", text)
        field = lb.stamp_field({"pytest tools/::a b": {}, "flake8": {}}, "0123456789abcdef")
        self.assertEqual(field, " known_red=flake8,pytest_tools/::a_b baseline=0123456789ab")
        many = lb.stamp_field({"s%02d" % i: {} for i in range(15)}, "m")
        self.assertIn(",+3 ", many + " ")
        self.assertEqual(lb.stamp_field({}, "m"), "")


class StampTests(unittest.TestCase):
    def test_stamp_text_and_gate_note(self):
        text = run_linters.stamp_text("changed", ["flake8"], known_red=" known_red=flake8 baseline=abc")
        self.assertIn("known_red=flake8 baseline=abc", text)
        self.assertIn("known red on main", lint_stamp_gate.known_red_note(text))
        self.assertIsNone(lint_stamp_gate.known_red_note(run_linters.stamp_text("changed", ["flake8"])))
        self.assertIsNone(lint_stamp_gate.refusal(text, ["a.py"]))

    def test_read_known_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.json")
            self.assertEqual(lint_batch.read_known_red(path), "")
            with open(path, "w") as handle:
                json.dump({"known_red": " known_red=a baseline=b"}, handle)
            self.assertEqual(lint_batch.read_known_red(path), " known_red=a baseline=b")


FAKE = r'''
import json, sys
with open(sys.argv[1], "w") as handle:
    json.dump({"selected": ["flake8", "pytest tools/[1of2]"],
               "steps": [{"name": "flake8", "returncode": 1}, {"name": "pytest tools/[1of2]", "returncode": 0}],
               "outputs": {"flake8": "x.py:1: E1 bad\n"}}, handle)
sys.exit(1)
'''


class RefreshTests(Env):
    def command(self, tree, results):
        script = os.path.join(self.tmp.name, "fake.py")
        with open(script, "w") as handle:
            handle.write(FAKE)
        self.trees_seen = getattr(self, "trees_seen", []) + [tree]
        return [sys.executable, script, results]

    def test_canary_records_a_clean_main_and_is_idempotent(self):
        work = helpers.make_repo(self.tmp.name)
        lines = []
        code = lb.refresh(work, "hams_demo", "hams_demo", environ=self.env, out=lines.append, command=self.command,
                          prepare=lambda tree: None)
        self.assertEqual(code, 0, lines)
        doc = lb.read_baseline("hams_demo", self.env)
        self.assertEqual(doc["main"], helpers.git(work, "rev-parse", "origin/main"))
        self.assertEqual(list(doc["items"]), ["flake8"])
        self.assertFalse(os.path.exists(self.trees_seen[0]), "the temporary tree is removed")
        calls = len(self.trees_seen)
        lb.refresh(work, "hams_demo", "hams_demo", environ=self.env, out=lines.append, command=self.command)
        self.assertEqual(len(self.trees_seen), calls, "current baseline: no second run")
        self.assertIn("is current", lines[-1])
        lb.refresh(work, "hams_demo", "hams_demo", environ=self.env, out=lines.append, command=self.command,
                   force=True, prepare=lambda tree: None)
        self.assertEqual(len(self.trees_seen), calls + 1)

    def test_a_run_that_reports_no_steps_keeps_the_old_baseline(self):
        work = helpers.make_repo(self.tmp.name)
        lb.write_baseline("hams_demo", "old", {"x": {"step": "x", "test": "", "signature": "s", "message": "m"}}, self.env)
        lines = []
        code = lb.refresh(work, "hams_demo", "hams_demo", environ=self.env, out=lines.append,
                          command=lambda tree, results: [sys.executable, "-c", "import sys; sys.exit(75)"],
                          prepare=lambda tree: None)
        self.assertEqual(code, 75)
        self.assertEqual(lb.read_baseline("hams_demo", self.env)["main"], "old")

    def test_baseline_command_show_and_main_shared_checkout(self):
        class Options:
            baseline_show = True
            baseline_refresh = False
            baseline_force = False
            remote = ""

        work = helpers.make_repo(self.tmp.name)
        self.assertEqual(run_linters.baseline_command(Options, work, work), 0)


if __name__ == "__main__":
    unittest.main()
