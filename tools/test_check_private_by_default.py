#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for check_private_by_default.py."""

import json
import os
import tempfile
import unittest

import check_private_by_default as cpd  # noqa: E402


def _addon(root, name, internal):
    os.makedirs(os.path.join(root, name))
    with open(os.path.join(root, name, cpd.EXPOSURE_FILE), "w", encoding="utf-8") as handle:
        json.dump({"internal": internal}, handle)


class CountTests(unittest.TestCase):
    def test_counts_methods_per_addon_and_skips_addons_without_a_file(self):
        with tempfile.TemporaryDirectory() as root:
            _addon(root, "alpha", {"a.b": ["x", "y"], "a.c": ["z"]})
            _addon(root, "beta", {})
            os.makedirs(os.path.join(root, "gamma"))
            self.assertEqual(cpd.count_listed_methods(root), {"alpha": 3, "beta": 0})


class CompareTests(unittest.TestCase):
    BASE = {"alpha": {"count": 3, "owner": "o", "tracking": "t"}}

    def test_growth_and_unbaselined_modules_fail(self):
        problems, _ = cpd.compare({"alpha": 4, "beta": 1}, self.BASE)
        self.assertEqual(len(problems), 2)
        self.assertIn("alpha", problems[0])
        self.assertIn("no baseline entry", problems[1])

    def test_equal_passes_and_a_new_module_at_zero_passes(self):
        self.assertEqual(cpd.compare({"alpha": 3, "beta": 0}, self.BASE), ([], []))

    def test_shrinking_is_a_note_not_a_failure(self):
        problems, notes = cpd.compare({"alpha": 1}, self.BASE)
        self.assertEqual(problems, [])
        self.assertIn("lower its baseline", notes[0])

    def test_a_stale_baseline_entry_is_a_note(self):
        _, notes = cpd.compare({}, self.BASE)
        self.assertIn("remove the entry", notes[0])


class BaselineTests(unittest.TestCase):
    def test_the_baseline_can_go_down_but_never_up_and_keeps_owners(self):
        previous = {"alpha": {"count": 3, "owner": "someone", "tracking": "todo"}}
        text = cpd.render_baseline({"alpha": 2, "beta": 0, "gamma": 5}, previous)
        entries = json.loads(text)["entries"]
        self.assertEqual(entries["alpha"], {"count": 2, "owner": "someone", "tracking": "todo"})
        self.assertNotIn("beta", entries)
        self.assertEqual(entries["gamma"]["owner"], cpd.DEFAULT_OWNER)
        with self.assertRaises(ValueError):
            cpd.render_baseline({"alpha": 4}, previous)

    def test_main_exit_codes(self):
        with tempfile.TemporaryDirectory() as root:
            _addon(root, "alpha", {"a.b": ["x"]})
            baseline = os.path.join(root, "baseline.json")
            self.assertEqual(cpd.main([root, "--baseline", baseline]), 1)
            self.assertEqual(cpd.main([root, "--baseline", baseline, "--write-baseline"]), 0)
            self.assertEqual(cpd.main([root, "--baseline", baseline]), 0)
            _addon(root, "beta", {"c.d": ["y"]})
            self.assertEqual(cpd.main([root, "--baseline", baseline]), 1)


if __name__ == "__main__":
    unittest.main()
