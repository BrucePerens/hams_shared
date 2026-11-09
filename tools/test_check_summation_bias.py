#!/usr/bin/env python3
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


def _run(repo, *args):
    result = subprocess.run(
        [sys.executable, _SCRIPT, *args], cwd=repo, capture_output=True, text=True, timeout=30
    )
    return result.returncode, result.stdout + result.stderr


class CheckSummationBiasTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _init_repo(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_staged_mode_flags_a_staged_shrink(self):
        _commit_file(self.tmp, "a.md", "x" * 1000)
        _write(os.path.join(self.tmp, "a.md"), "x" * 100)
        _git(self.tmp, "add", "a.md")
        code, out = _run(self.tmp, "--staged")
        self.assertEqual(code, 1, out)
        self.assertIn("SUMMATION BIAS DETECTED in a.md", out)

    def test_staged_mode_ignores_a_shrunk_file_that_is_not_staged(self):
        # Another session's unrelated edit in the shared working tree must not block this commit.
        _commit_file(self.tmp, "a.md", "x" * 1000)
        _commit_file(self.tmp, "b.md", "y" * 1000)
        _write(os.path.join(self.tmp, "a.md"), "x" * 100)
        _write(os.path.join(self.tmp, "b.md"), "y" * 1001)
        _git(self.tmp, "add", "b.md")
        code, out = _run(self.tmp, "--staged")
        self.assertEqual(code, 0, out)

    def test_staged_mode_judges_the_staged_content_not_the_working_tree(self):
        _commit_file(self.tmp, "a.md", "x" * 1000)
        _write(os.path.join(self.tmp, "a.md"), "x" * 990)
        _git(self.tmp, "add", "a.md")
        _write(os.path.join(self.tmp, "a.md"), "x" * 10)  # a later, unstaged gutting
        code, out = _run(self.tmp, "--staged")
        self.assertEqual(code, 0, out)

    def test_staged_mode_ignores_a_staged_deletion(self):
        _commit_file(self.tmp, "a.md", "x" * 1000)
        _git(self.tmp, "rm", "-q", "a.md")
        code, out = _run(self.tmp, "--staged")
        self.assertEqual(code, 0, out)

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

    def test_a_gutted_file_is_still_flagged_when_run_from_a_subdirectory(self):
        # Real bug found 2026-09-10: `git diff --name-only HEAD` always reports paths
        # relative to the REPO ROOT no matter where git was invoked from, but the old code
        # checked os.path.exists(filepath)/os.path.getsize(filepath) against the raw,
        # repo-root-relative path -- which only resolves correctly when the SCRIPT's own cwd
        # happens to already be the repo root. Confirmed this script's real, primary
        # invocation (as the actual `.git/hooks/pre-commit`, per this file's own module
        # docstring) is safe from this specific angle -- git always runs hooks with cwd
        # already at the top-level working directory, verified directly against a real git
        # hook in this same investigation -- but ANY other invocation (a manual re-run, or
        # run_linters.py's own subprocess call, from a working directory that is not exactly
        # the repo root) silently treated every real, modified file as "deleted" and reported
        # zero violations, with no warning that the check had effectively not run at all.
        # This test reproduces that non-hook invocation shape directly.
        _commit_file(self.tmp, "subdir/foo.py", "x = 1\n" * 100)
        _write(os.path.join(self.tmp, "subdir", "foo.py"), "x = 1\n")
        result = subprocess.run(
            [sys.executable, _SCRIPT],
            cwd=os.path.join(self.tmp, "subdir"),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("SUMMATION BIAS DETECTED", result.stdout)


def _lines(n, prefix="line"):
    return "".join(f"{prefix} {i:03d} with a good many words of explanatory text\n" for i in range(n))


class MergeCommitTests(unittest.TestCase):
    """A merge commit is judged against the merge result, not the first parent (the branch author's side)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _init_repo(self.tmp)
        _git(self.tmp, "checkout", "-q", "-b", "trunk")
        self.base = _lines(100)
        _commit_file(self.tmp, "a.md", self.base, "base a")
        _commit_file(self.tmp, "other.md", "o\n", "base other")
        _git(self.tmp, "checkout", "-q", "-b", "feature")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reworded(self):
        # Main shortens the first 60 lines (about 15 percent of the file) and leaves the tail alone.
        lines = self.base.splitlines(True)
        return "".join(l[:-20] + "\n" for l in lines[:60]) + "".join(lines[60:])

    def _main_rewords(self, content):
        _git(self.tmp, "checkout", "-q", "trunk")
        _commit_file(self.tmp, "a.md", content, "main rewords a")
        _git(self.tmp, "checkout", "-q", "feature")

    def _feature_touches_other_file(self):
        _commit_file(self.tmp, "other.md", "o\nmore\n", "feature edit")

    def _merge(self, expect_conflict=False):
        res = subprocess.run(
            ["git", "merge", "--no-commit", "--no-ff", "trunk"], cwd=self.tmp, capture_output=True, text=True
        )
        self.assertEqual(res.returncode != 0, expect_conflict, res.stdout + res.stderr)

    def test_ordinary_commit_after_a_merge_is_still_strict(self):
        self._main_rewords(_lines(100)[: len(self.base) * 8 // 10])
        self._feature_touches_other_file()
        self._merge()
        _git(self.tmp, "commit", "-q", "-m", "merge", "--no-verify")
        _write(os.path.join(self.tmp, "a.md"), _lines(10))
        _git(self.tmp, "add", "a.md")
        code, out = _run(self.tmp, "--staged")
        self.assertEqual(code, 1, out)
        self.assertIn("SUMMATION BIAS DETECTED in a.md", out)

    def test_shrink_that_came_from_main_is_not_flagged_in_a_merge(self):
        shorter = self._reworded()
        self._main_rewords(shorter)
        self._feature_touches_other_file()
        self._merge()
        # The same content committed ordinarily on the feature branch is a 5+ percent shrink and is caught.
        self.assertLess(len(shorter), len(self.base) * 0.95)
        code, out = _run(self.tmp, "--staged")
        self.assertEqual(code, 0, out)

    def test_clean_merge_of_mains_shrink_with_an_unrelated_author_edit_is_not_flagged(self):
        shorter = self._reworded()
        self._main_rewords(shorter)
        _commit_file(self.tmp, "a.md", self.base + "# author appended a line\n", "feature edits a")
        self._merge()
        code, out = _run(self.tmp, "--staged")
        self.assertEqual(code, 0, out)

    def test_author_shrinking_below_mains_version_in_a_merge_is_flagged(self):
        shorter = self._reworded()
        self._main_rewords(shorter)
        self._feature_touches_other_file()
        self._merge()
        _write(os.path.join(self.tmp, "a.md"), shorter[: len(shorter) // 2])
        _git(self.tmp, "add", "a.md")
        code, out = _run(self.tmp, "--staged")
        self.assertEqual(code, 1, out)
        self.assertIn("SUMMATION BIAS DETECTED in a.md", out)

    def test_author_shrink_on_top_of_a_clean_merge_is_flagged(self):
        shorter = self._reworded()
        self._main_rewords(shorter)
        _commit_file(self.tmp, "a.md", self.base + "# author appended a line\n", "feature edits a")
        self._merge()
        with open(os.path.join(self.tmp, "a.md"), encoding="utf-8") as f:
            merged = f.read()
        _write(os.path.join(self.tmp, "a.md"), merged[: len(merged) * 8 // 10])
        _git(self.tmp, "add", "a.md")
        code, out = _run(self.tmp, "--staged")
        self.assertEqual(code, 1, out)

    def test_conflict_resolution_that_drops_lines_is_flagged(self):
        head, tail = self.base.splitlines(True)[:50], self.base.splitlines(True)[50:]
        self._main_rewords("".join(head) + "".join(l.replace("line", "MAIN") for l in tail))
        _commit_file(self.tmp, "a.md", "".join(head) + "".join(l.replace("line", "FEAT") for l in tail), "feature edits a")
        self._merge(expect_conflict=True)
        _write(os.path.join(self.tmp, "a.md"), "".join(head))  # resolved by throwing the contested half away
        _git(self.tmp, "add", "a.md")
        code, out = _run(self.tmp, "--staged")
        self.assertEqual(code, 1, out)
        self.assertIn("SUMMATION BIAS DETECTED in a.md", out)

    def test_conflict_resolved_to_mains_version_is_not_flagged(self):
        shorter = self._reworded()
        self._main_rewords(shorter)
        _commit_file(self.tmp, "a.md", self.base.replace("line 000", "LINE 000"), "feature edits a")
        # Conflicts only if the hunks touch; force that by rewording line 000 on main too.
        self._merge(expect_conflict=True)
        _git(self.tmp, "checkout", "--theirs", "a.md")
        _git(self.tmp, "add", "a.md")
        code, out = _run(self.tmp, "--staged")
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
