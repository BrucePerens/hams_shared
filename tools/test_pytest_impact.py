#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tests for pytest_impact.py: which test files of a pytest lint step a `--changed` run keeps. Throwaway trees of small Python files.

Tests [@ANCHOR: pytest_impact:select_tests]
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import pytest_impact  # noqa: E402


class Tree(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="pytestimpact-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, name, text=""):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def select(self, tests, changed, also=()):
        selected, skipped, why = pytest_impact.select_tests(self.root, tests, changed, also)
        return [os.path.basename(p) for p in selected], [os.path.basename(p) for p in skipped], why


class SelectionTests(Tree):
    def setUp(self):
        super().setUp()
        self.lib = self.write("lib.py", "def f():\n    return 1\n")
        self.mid = self.write("mid.py", "import lib\n")
        self.test_lib = self.write("test_lib.py", "import lib\n")
        self.test_mid = self.write("test_mid.py", "from mid import g\n")
        self.test_other = self.write("test_other.py", "import os\n")
        self.tests = [self.test_lib, self.test_mid, self.test_other]

    def test_a_changed_module_selects_the_tests_that_import_it_directly_or_through_another_module(self):
        selected, skipped, _ = self.select(self.tests, [self.lib])
        self.assertEqual((selected, skipped), (["test_lib.py", "test_mid.py"], ["test_other.py"]))
        selected, skipped, _ = self.select(self.tests, [self.mid])
        self.assertEqual((selected, skipped), (["test_mid.py"], ["test_lib.py", "test_other.py"]))

    def test_a_changed_test_file_selects_itself(self):
        self.assertEqual(self.select(self.tests, [self.test_other])[0], ["test_other.py"])

    def test_a_word_in_a_comment_or_a_docstring_is_not_a_dependency(self):
        self.write("test_prose.py", '"""Mentions lib in prose."""\n# lib again\nimport os\n')
        selected, skipped, _ = self.select(self.tests + [os.path.join(self.root, "test_prose.py")], [self.lib])
        self.assertIn("test_prose.py", skipped)

    def test_a_file_name_in_a_string_is_a_dependency_and_so_is_a_data_file_read_by_name(self):
        data = self.write("rules.json", "{}")
        self.write("test_reads.py", 'open("rules.json")\n')
        selected, _, _ = self.select(self.tests + [os.path.join(self.root, "test_reads.py")], [data])
        self.assertEqual(selected, ["test_reads.py"])

    def test_a_script_run_by_name_passes_the_dependency_on_to_the_test_that_imports_the_runner(self):
        script = self.write("tool.py", "print(1)\n")
        self.write("runner.py", 'subprocess.run(["python3", "tool.py"])\n')
        self.write("test_runner.py", "import runner\n")
        selected, _, _ = self.select(self.tests + [os.path.join(self.root, "test_runner.py")], [script])
        self.assertEqual(selected, ["test_runner.py"])

    def test_a_test_that_scans_the_tree_itself_always_runs(self):
        scanner = self.write("test_scan.py", "import glob, os\nHERE = os.path.dirname(__file__)\nglob.glob(os.path.join(HERE, '*.py'))\n")
        scratch = self.write("test_scratch.py", "import os\nos.listdir(self.tmp)\n")
        selected, skipped, _ = self.select(self.tests + [scanner, scratch], [self.test_other])
        self.assertEqual(selected, ["test_other.py", "test_scan.py"])
        self.assertIn("test_scratch.py", skipped)

    def test_everything_runs_when_a_change_cannot_be_traced(self):
        for changed, reason in (
            ([self.write("conftest.py")], "conftest.py"),
            ([self.write("requirements.txt")], "requirements.txt"),
            ([self.write("lonely.py")], "mentioned by no file"),
            ([], "no changed file"),
            ([os.path.join(os.path.dirname(self.root), "elsewhere.py")], "no changed file"),
        ):
            selected, skipped, why = self.select(self.tests, changed)
            self.assertEqual((len(selected), skipped), (3, []), changed)
            self.assertIn(reason, why)

    def test_a_changed_input_outside_the_root_selects_everything_and_other_paths_do_not_matter(self):
        loose = os.path.join(os.path.dirname(self.root), "loose.py")
        self.assertEqual(self.select(self.tests, [self.lib, loose], also=[loose])[1], [])
        self.assertEqual(self.select(self.tests, [self.lib, loose])[1], ["test_other.py"], "a path that is no input of the step is ignored")

    def test_an_environment_switch_selects_everything(self):
        with mock.patch.dict(os.environ, {"HAMS_LINT_TEST_IMPACT": "0"}):
            self.assertEqual(self.select(self.tests, [self.lib])[1], [])

    def test_a_deleted_test_file_leaves_nothing_to_run_and_a_deleted_module_selects_its_importers(self):
        gone = os.path.join(self.root, "test_gone.py")
        self.assertEqual(self.select(self.tests, [gone, self.test_lib])[0], ["test_lib.py"])
        os.remove(self.lib)
        selected, _, _ = self.select(self.tests, [self.lib])
        self.assertEqual(selected, ["test_lib.py", "test_mid.py"])

    def test_the_note_names_the_skipped_files(self):
        selected, skipped, why = pytest_impact.select_tests(self.root, self.tests, [self.mid])
        note = pytest_impact.describe("tools/", self.root, selected, skipped, why)
        self.assertIn("skipped 2: test_lib.py, test_other.py", note)


if __name__ == "__main__":
    unittest.main()
