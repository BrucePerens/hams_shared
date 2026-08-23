#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_summation_bias.py.

This is the only script in hams_shared/tools/ with a live, automatic,
every-commit enforcement path -- it's the entirety of .git/hooks/pre-commit
in this codebase. It has no extracted pure functions and reads real git
state (git diff --name-only HEAD, git cat-file -s HEAD:<path>), so these
tests build a real temp git repository per test (git init, a commit, then a
working-tree edit) rather than bare files on disk, and run the script as a
real subprocess with cwd set to that repo.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_summation_bias.py")


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def _init_repo(repo):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _commit_file(repo, relpath, content, message="add"):
    _write(os.path.join(repo, relpath), content)
    _git(repo, "add", relpath)
    _git(repo, "commit", "-q", "-m", message)


def _run(repo):
    result = subprocess.run(
        [sys.executable, _SCRIPT], cwd=repo, capture_output=True, text=True, timeout=30
    )
    return result.returncode, result.stdout + result.stderr


class CheckSummationBiasTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _init_repo(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_python_file_shrunk_more_than_5_percent_is_flagged(self):
        _commit_file(self.tmp, "foo.py", "x = 1\n" * 100)
        _write(os.path.join(self.tmp, "foo.py"), "x = 1\n" * 50)
        code, out = _run(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("SUMMATION BIAS DETECTED", out)
        self.assertIn("foo.py", out)

    def test_a_python_file_shrunk_by_a_trivial_amount_is_not_flagged(self):
        _commit_file(self.tmp, "foo.py", "x = 1\n" * 100)
        _write(os.path.join(self.tmp, "foo.py"), "x = 1\n" * 98)
        code, out = _run(self.tmp)
        self.assertEqual(code, 0, out)

    def test_a_file_that_grows_is_never_flagged(self):
        _commit_file(self.tmp, "foo.py", "x = 1\n" * 10)
        _write(os.path.join(self.tmp, "foo.py"), "x = 1\n" * 100)
        code, out = _run(self.tmp)
        self.assertEqual(code, 0, out)

    def test_an_untracked_extension_is_never_checked_even_if_gutted(self):
        _commit_file(self.tmp, "foo.txt", "x\n" * 100)
        _write(os.path.join(self.tmp, "foo.txt"), "x\n")
        code, out = _run(self.tmp)
        self.assertEqual(code, 0, out)

    def test_a_newly_created_file_not_yet_in_head_is_never_flagged(self):
        # A commit is needed for `git diff --name-only HEAD` to have a HEAD
        # to diff against at all -- create one unrelated file, then add a
        # brand-new second file that was never committed.
        _commit_file(self.tmp, "unrelated.py", "pass\n")
        _write(os.path.join(self.tmp, "new_file.py"), "x = 1\n")
        _git(self.tmp, "add", "new_file.py")
        code, out = _run(self.tmp)
        self.assertEqual(code, 0, out)

    def test_a_deleted_file_is_never_flagged(self):
        _commit_file(self.tmp, "foo.py", "x = 1\n" * 100)
        os.remove(os.path.join(self.tmp, "foo.py"))
        code, out = _run(self.tmp)
        self.assertEqual(code, 0, out)

    def test_not_a_git_repository_at_all_exits_cleanly(self):
        no_git_dir = tempfile.mkdtemp()
        try:
            code, out = _run(no_git_dir)
            self.assertEqual(code, 0, out)
        finally:
            shutil.rmtree(no_git_dir, ignore_errors=True)

    def test_a_markdown_file_shrunk_more_than_5_percent_is_flagged(self):
        _commit_file(self.tmp, "README.md", "# Notes\n" + ("detail line\n" * 100))
        _write(os.path.join(self.tmp, "README.md"), "# Notes\n")
        code, out = _run(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("README.md", out)

    def test_a_json_file_shrunk_more_than_5_percent_is_flagged(self):
        _commit_file(self.tmp, "data.json", '{"a": ' + "1" * 200 + "}")
        _write(os.path.join(self.tmp, "data.json"), "{}")
        code, out = _run(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("data.json", out)

    def test_multiple_shrunk_files_are_all_reported_not_just_the_first(self):
        _commit_file(self.tmp, "a.py", "x = 1\n" * 100)
        _commit_file(self.tmp, "b.py", "x = 1\n" * 100)
        _write(os.path.join(self.tmp, "a.py"), "x = 1\n")
        _write(os.path.join(self.tmp, "b.py"), "x = 1\n")
        code, out = _run(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("a.py", out)
        self.assertIn("b.py", out)

    def test_exactly_at_the_five_percent_boundary_is_not_flagged(self):
        # The check is strictly "> 0.05", so an exact 5.0% reduction must
        # NOT be flagged -- verifies the boundary is the documented open
        # comparison, not an off-by-one closed one.
        old_content = "x" * 1000
        new_content = "x" * 950  # exactly 5.0% smaller
        _commit_file(self.tmp, "foo.py", old_content)
        _write(os.path.join(self.tmp, "foo.py"), new_content)
        code, out = _run(self.tmp)
        self.assertEqual(code, 0, out)

    def test_just_over_the_five_percent_boundary_is_flagged(self):
        old_content = "x" * 1000
        new_content = "x" * 949  # 5.1% smaller
        _commit_file(self.tmp, "foo.py", old_content)
        _write(os.path.join(self.tmp, "foo.py"), new_content)
        code, out = _run(self.tmp)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
