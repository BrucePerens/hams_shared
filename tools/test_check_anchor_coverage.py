#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_anchor_coverage.py (ADR 0090 decision 4, Stage 3).

`hams_shared/tools/` is excluded from both `check_function_test_anchors.py`'s own ratchet and
`verify_anchors.py`'s scan (same as every other file directly in this directory) -- these tests
exist because the tool itself needs real coverage, not because `# [@ANCHOR: ...]` citations apply
to code in this directory.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_anchor_coverage as cac  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _init_git_repo(root):
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)


class LoadCoverageFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_real_coverage_json_files_dict_is_returned(self):
        path = os.path.join(self.tmp, "coverage.json")
        _write(
            path,
            json.dumps(
                {"files": {"foo.py": {"executed_lines": [1, 2], "missing_lines": [3]}}}
            ),
        )
        self.assertEqual(
            cac.load_coverage_files(path),
            {"foo.py": {"executed_lines": [1, 2], "missing_lines": [3]}},
        )

    def test_a_missing_files_key_returns_an_empty_dict(self):
        path = os.path.join(self.tmp, "coverage.json")
        _write(path, json.dumps({"meta": {}}))
        self.assertEqual(cac.load_coverage_files(path), {})


class CheckAnchorCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_anchored_function_with_an_executed_line_is_not_a_gap(self):
        path = os.path.join(self.tmp, "foo.py")
        _write(path, "def bar():\n    # [@ANCHOR: COMM_bar]\n    return 1\n")
        _init_git_repo(self.tmp)
        coverage_files = {"foo.py": {"executed_lines": [3], "missing_lines": []}}
        gaps = cac.check_anchor_coverage(self.tmp, coverage_files)
        self.assertEqual(gaps, [])

    def test_an_anchored_function_with_zero_executed_lines_is_a_gap(self):
        path = os.path.join(self.tmp, "foo.py")
        _write(path, "def bar():\n    # [@ANCHOR: COMM_bar]\n    return 1\n")
        _init_git_repo(self.tmp)
        # Line 3 (`return 1`) is a real statement line coverage.py tracked
        # for this file, but it never executed -- the binary "touched at
        # all" gap ADR 0090 decision 4 names.
        coverage_files = {"foo.py": {"executed_lines": [], "missing_lines": [3]}}
        gaps = cac.check_anchor_coverage(self.tmp, coverage_files)
        self.assertEqual(len(gaps), 1)
        identity, start, end = gaps[0]
        self.assertEqual(identity, "foo.py::bar")

    def test_an_unanchored_function_is_not_reported_even_if_uncovered(self):
        path = os.path.join(self.tmp, "foo.py")
        _write(path, "def bar():\n    return 1\n")
        _init_git_repo(self.tmp)
        coverage_files = {"foo.py": {"executed_lines": [], "missing_lines": [2]}}
        gaps = cac.check_anchor_coverage(self.tmp, coverage_files)
        self.assertEqual(gaps, [])

    def test_a_file_absent_from_coverage_json_is_silently_skipped(self):
        path = os.path.join(self.tmp, "foo.py")
        _write(path, "def bar():\n    # [@ANCHOR: COMM_bar]\n    return 1\n")
        _init_git_repo(self.tmp)
        gaps = cac.check_anchor_coverage(self.tmp, {})
        self.assertEqual(gaps, [])

    def test_a_stub_with_no_real_statement_lines_in_scope_is_skipped(self):
        path = os.path.join(self.tmp, "foo.py")
        _write(path, "def bar():\n    # [@ANCHOR: COMM_bar]\n    ...\n")
        _init_git_repo(self.tmp)
        # coverage.py itself never assigned this file's docstring/ellipsis
        # line to either list -- nothing this run could tell us.
        coverage_files = {"foo.py": {"executed_lines": [], "missing_lines": []}}
        gaps = cac.check_anchor_coverage(self.tmp, coverage_files)
        self.assertEqual(gaps, [])

    def test_partial_execution_of_a_span_still_counts_as_touched(self):
        path = os.path.join(self.tmp, "foo.py")
        _write(
            path,
            "def bar(x):\n"
            "    # [@ANCHOR: COMM_bar]\n"
            "    if x:\n"
            "        return 1\n"
            "    return 2\n",
        )
        _init_git_repo(self.tmp)
        # Line 4 (the `if x` branch) never ran, but line 3 and line 5 did
        # -- ADR 0090 decision 4's binary gate only asks "touched at all,"
        # not "every line," so this is deliberately NOT a Stage 3 gap (it's
        # exactly the future branch-coverage follow-on's job, not this
        # gate's).
        coverage_files = {
            "foo.py": {"executed_lines": [3, 5], "missing_lines": [4]}
        }
        gaps = cac.check_anchor_coverage(self.tmp, coverage_files)
        self.assertEqual(gaps, [])


if __name__ == "__main__":
    unittest.main()
