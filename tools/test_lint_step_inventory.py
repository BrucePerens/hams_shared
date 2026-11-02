#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for lint_step_inventory.py: collected-test sets, duplicate detection, before/after proof."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lint_exec  # noqa: E402
import lint_step_inventory as inv  # noqa: E402


class CompareTests(unittest.TestCase):
    def test_identical_sets_with_merged_steps_pass(self):
        before = {"pytest a": ["a/t.py::one", "a/t.py::two"], "pytest a/tests": ["a/tests/t.py::three"]}
        after = {"pytest a": ["a/t.py::one", "a/t.py::two", "a/tests/t.py::three"]}
        problems, left, right = inv.compare(before, after)
        self.assertEqual(problems, [])
        self.assertEqual((left, right), (3, 3))

    def test_lost_test_is_reported(self):
        problems, _, _ = inv.compare({"x": ["t::a", "t::b"]}, {"x": ["t::a"]})
        self.assertEqual(problems, ["LOST after the change: t::b"])

    def test_new_test_is_reported(self):
        problems, _, _ = inv.compare({"x": ["t::a"]}, {"x": ["t::a", "t::b"]})
        self.assertEqual(problems, ["NEW after the change: t::b"])

    def test_allow_new_accepts_only_the_changes_own_tests_and_never_a_lost_one(self):
        problems, _, _ = inv.compare({"x": ["t::a"]}, {"x": ["t::a", "new_test.py::b"]}, allow_new=["new_test.py"])
        self.assertEqual(problems, [])
        problems, _, _ = inv.compare({"x": ["new_test.py::a"]}, {"x": []}, allow_new=["new_test.py"])
        self.assertEqual(problems, ["LOST after the change: new_test.py::a"])

    def test_duplicate_in_after_is_reported(self):
        problems, _, _ = inv.compare({"x": ["t::a"]}, {"x": ["t::a"], "y": ["t::a"]})
        self.assertEqual(len(problems), 1)
        self.assertIn("DUPLICATE in after (x, y): t::a", problems[0])

    def test_duplicates_of_empty_when_disjoint(self):
        self.assertEqual(inv.duplicates_of({"x": ["a"], "y": ["b"]}), {})


class CollectTests(unittest.TestCase):
    def test_collect_one_makes_ids_tree_relative(self):
        with tempfile.TemporaryDirectory() as tree:
            sub = os.path.join(tree, "d")
            os.makedirs(sub)
            with open(os.path.join(sub, "test_x.py"), "w", encoding="utf-8") as handle:
                handle.write("def test_a():\n    pass\n\n\ndef test_b():\n    pass\n")
            step = {
                "name": "pytest d",
                "argv": [sys.executable, "-m", "pytest", "-q", os.path.join(sub, "test_x.py")],
                "cwd": sub,
                "env_delta": {},
            }
            name, ids, code, _summary, _err = inv._collect_one(step, tree)
        self.assertEqual(name, "pytest d")
        self.assertEqual(code, 0)
        self.assertEqual(ids, ["d/test_x.py::test_a", "d/test_x.py::test_b"])

    def test_map_labels_files_outside_the_tree(self):
        with tempfile.TemporaryDirectory() as tree, tempfile.TemporaryDirectory() as shared:
            with open(os.path.join(shared, "test_y.py"), "w", encoding="utf-8") as handle:
                handle.write("def test_a():\n    pass\n")
            step = {"name": "pytest tools", "argv": [sys.executable, "-m", "pytest", "-q", os.path.join(shared, "test_y.py")],
                    "cwd": shared, "env_delta": {}}
            _name, ids, _code, _summary, _err = inv._collect_one(step, tree, [(shared, "hams_shared")])
        self.assertEqual(ids, ["hams_shared/test_y.py::test_a"])

    def test_non_pytest_step_is_not_collected(self):
        self.assertFalse(inv.is_pytest_step({"argv": ["/usr/bin/python3", "tools/check_x.py"]}))
        self.assertFalse(inv.is_pytest_step({"argv": None}))
        self.assertTrue(inv.is_pytest_step({"argv": ["py", "-m", "pytest", "-q"]}))

    def test_compare_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = os.path.join(tmp, "a.json"), os.path.join(tmp, "b.json")
            with open(first, "w", encoding="utf-8") as handle:
                json.dump({"s": ["t::a"]}, handle)
            with open(second, "w", encoding="utf-8") as handle:
                json.dump({"s": ["t::a"]}, handle)
            self.assertEqual(inv.main(["compare", first, second]), 0)
            with open(second, "w", encoding="utf-8") as handle:
                json.dump({"s": []}, handle)
            self.assertEqual(inv.main(["compare", first, second]), 1)


class StepToDictTests(unittest.TestCase):
    def test_environment_is_reduced_to_the_difference_so_secrets_are_not_listed(self):
        os.environ["HAMS_INVENTORY_TEST_SECRET"] = "s3cret"
        try:
            env = dict(os.environ, ODOO_URL="http://x.invalid")
            step = lint_exec.Step(name="n", argv=["a"], env=env, roots=("/r",), suffixes=frozenset({".py"}))
            data = lint_exec.step_to_dict(step)
        finally:
            del os.environ["HAMS_INVENTORY_TEST_SECRET"]
        self.assertEqual(data["env_delta"], {"ODOO_URL": "http://x.invalid"})
        self.assertNotIn("s3cret", json.dumps(data))
        self.assertEqual(data["suffixes"], [".py"])


if __name__ == "__main__":
    unittest.main()
