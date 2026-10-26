#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_staged_files.py and the fail-closed lookup in git-hooks/pre-commit, against real temporary git
repositories and the real hook script.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_TOOLS = os.path.dirname(os.path.abspath(__file__))
_HOOK = os.path.join(_TOOLS, "git-hooks", "pre-commit")


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


def _init(repo):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _run(args, repo):
    return subprocess.run(args, cwd=repo, capture_output=True, text=True)


class StagedFilesTests(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, True)
        _init(self.repo)

    def _check(self):
        return _run([sys.executable, os.path.join(_TOOLS, "check_staged_files.py")], self.repo)

    def test_a_small_ordinary_file_passes(self):
        _write(os.path.join(self.repo, "a/b.py"), b"print(1)\n")
        _git(self.repo, "add", "a/b.py")
        self.assertEqual(self._check().returncode, 0)

    def test_a_file_over_the_limit_is_refused(self):
        _write(os.path.join(self.repo, "big.bin"), b"x" * (5 * 1024 * 1024 + 1))
        _git(self.repo, "add", "big.bin")
        result = self._check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("big.bin", result.stderr)

    def test_a_file_under_a_target_directory_is_refused_at_any_depth(self):
        _write(os.path.join(self.repo, "daemons/x/target/debug/app"), b"y")
        _git(self.repo, "add", "-f", "daemons/x/target/debug/app")
        result = self._check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("build output", result.stderr)

    def test_a_file_merely_named_target_is_allowed(self):
        _write(os.path.join(self.repo, "docs/target"), b"z")
        _write(os.path.join(self.repo, "targets/readme.md"), b"z")
        _git(self.repo, "add", "docs/target", "targets/readme.md")
        self.assertEqual(self._check().returncode, 0)

    def test_deleting_a_large_file_is_allowed(self):
        _write(os.path.join(self.repo, "big.bin"), b"x" * (5 * 1024 * 1024 + 1))
        _git(self.repo, "add", "big.bin")
        _git(self.repo, "commit", "-q", "--no-verify", "-m", "add")
        _git(self.repo, "rm", "-q", "big.bin")
        self.assertEqual(self._check().returncode, 0)


class HookFailsClosedTests(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, True)
        _init(self.repo)
        _write(os.path.join(self.repo, "a.txt"), b"a")
        _git(self.repo, "add", "a.txt")

    def test_a_checkout_without_the_checkers_refuses_the_commit(self):
        result = _run(["bash", _HOOK], self.repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not found", result.stderr)

    def test_checkers_in_tools_run_and_a_clean_commit_passes(self):
        for name in ("check_summation_bias.py", "check_staged_files.py"):
            shutil.copy(os.path.join(_TOOLS, name), self._tools(name))
        self.assertEqual(_run(["bash", _HOOK], self.repo).returncode, 0)

    def _tools(self, name):
        os.makedirs(os.path.join(self.repo, "tools"), exist_ok=True)
        return os.path.join(self.repo, "tools", name)

    def test_a_worktree_with_an_empty_hams_shared_uses_the_main_checkout(self):
        for name in ("check_summation_bias.py", "check_staged_files.py"):
            shutil.copy(os.path.join(_TOOLS, name), self._shared_tools(name))
        _git(self.repo, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "base")
        worktree = os.path.join(self.repo, ".claude", "worktrees", "w")
        _git(self.repo, "worktree", "add", "-q", "-b", "w", worktree)
        os.makedirs(os.path.join(worktree, "hams_shared"), exist_ok=True)
        _write(os.path.join(worktree, "big.bin"), b"x" * (5 * 1024 * 1024 + 1))
        _git(worktree, "add", "big.bin")
        result = _run(["bash", _HOOK], worktree)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("big.bin", result.stderr)

    def _shared_tools(self, name):
        os.makedirs(os.path.join(self.repo, "hams_shared", "tools"), exist_ok=True)
        return os.path.join(self.repo, "hams_shared", "tools", name)


if __name__ == "__main__":
    unittest.main()
