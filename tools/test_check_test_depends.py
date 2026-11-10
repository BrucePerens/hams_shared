#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for check_test_depends.py, with negative controls: a bad entry injected into a real manifest on disk must fail."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import check_test_depends as ctd  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_test_depends.py")


def _write(repo, name, depends, test_depends=None):
    path = os.path.join(repo, name)
    os.makedirs(path, exist_ok=True)
    body = {"depends": depends}
    if test_depends is not None:
        body["test_depends"] = test_depends
    with open(os.path.join(path, "__manifest__.py"), "w", encoding="utf-8") as handle:
        handle.write(repr(body) + "\n")


class CheckTestDependsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "hams_open")
        _write(self.repo, "base_mod", ["base"])
        _write(self.repo, "mid", ["base_mod"])
        _write(self.repo, "other", ["base"])
        _write(self.repo, "user", ["base_mod"], ["other"])

    def run_checker(self):
        return subprocess.run([sys.executable, _SCRIPT, self.repo], capture_output=True, text=True)

    def inject(self, name, depends, test_depends):
        _write(self.repo, name, depends, test_depends)
        return self.run_checker()

    def test_clean_tree_passes(self):
        result = self.run_checker()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unknown_module_fails(self):
        result = self.inject("user", ["base_mod"], ["no_such_module"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("UNKNOWN test_depends", result.stdout)

    def test_redundant_entry_fails(self):
        result = self.inject("user", ["base_mod"], ["base_mod"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("REDUNDANT test_depends", result.stdout)

    def test_self_reference_fails(self):
        result = self.inject("user", ["base_mod"], ["user"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("SELF-REFERENTIAL", result.stdout)

    def test_a_companion_that_depends_on_the_module_is_allowed(self):
        # ham_events -> ham_club_management (which depends on ham_events) is the real case: installed on top of the module.
        result = self.inject("base_mod", ["base"], ["mid"])
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_cycle_through_test_depends_edges_fails(self):
        _write(self.repo, "other", ["base"], ["user"])  # user -> other (test) and other -> user (test)
        result = self.run_checker()
        self.assertEqual(result.returncode, 1)
        self.assertIn("CYCLIC test_depends", result.stdout)

    def test_check_entries_pure(self):
        self.assertEqual(ctd.check_entries({"a": [], "b": []}, {"a": ["b"]}), [])
        self.assertEqual(ctd.check_entries({"a": [], "b": ["a"]}, {"a": ["b"]}), [])
        self.assertEqual(len(ctd.check_entries({"a": [], "b": []}, {"a": ["b"], "b": ["a"]})), 2)


if __name__ == "__main__":
    unittest.main()
