#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for worktree_hygiene.py against real temporary git repositories (a bare origin, a clone, real worktrees)."""

import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import worktree_hygiene as wh  # noqa: E402


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout.strip()


def _commit(repo, name, text="x"):
    with open(os.path.join(repo, name), "w") as handle:
        handle.write(text)
    _git(repo, "add", name)
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", f"add {name}")


class WorktreeHygieneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        origin = os.path.join(base, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True)
        self.repo = os.path.join(base, "repo")
        subprocess.run(["git", "clone", "-q", origin, self.repo], check=True, capture_output=True)
        _git(self.repo, "checkout", "-qB", "main")
        _commit(self.repo, "a")
        _git(self.repo, "push", "-q", "-u", "origin", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def worktree(self, name, commits=0, dirty=False):
        path = os.path.join(self.tmp.name, name)
        _git(self.repo, "worktree", "add", "-q", "-b", name, path, "origin/main")
        for i in range(commits):
            _commit(path, f"{name}{i}")
        if dirty:
            with open(os.path.join(path, "scratch"), "w") as handle:
                handle.write("unsaved")
        return path

    def test_merged_clean_worktree_and_its_branch_are_removed_only_with_apply(self):
        path = self.worktree("done")
        self.assertEqual(wh.run(self.repo, False, 1800, {}, now=time.time() + 3600)["removed"], [(path, "done")])
        self.assertTrue(os.path.isdir(path))
        wh.run(self.repo, True, 1800, {}, now=time.time() + 3600)
        self.assertFalse(os.path.isdir(path))
        self.assertNotIn("done", _git(self.repo, "branch", "--format=%(refname:short)"))

    def test_a_worktree_or_branch_made_a_moment_ago_is_not_removed_as_merged(self):
        path = self.worktree("just-made")
        _git(self.repo, "branch", "also-new")
        report = wh.run(self.repo, True, 1800, {})
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(report["removed"], [])
        self.assertEqual(report["deleted"], [])
        self.assertEqual({branch for _, branch in report["kept"]}, {"just-made"})
        self.assertIn("also-new", _git(self.repo, "branch", "--format=%(refname:short)"))

    def test_a_squash_merged_branch_is_recognised_by_its_pull_request_head(self):
        path = self.worktree("squashed", commits=2)
        tip = _git(path, "rev-parse", "HEAD")
        report = wh.run(self.repo, True, 1800, {"squashed": {tip}}, now=time.time() + 3600)
        self.assertEqual(report["removed"], [(path, "squashed")])
        self.assertEqual(report["stale"], [])

    def test_dirty_or_unmerged_work_is_never_removed_and_is_stale_after_the_limit(self):
        dirty = self.worktree("dirty", dirty=True)
        ahead = self.worktree("ahead", commits=1)
        later = time.time() + 3600
        report = wh.run(self.repo, True, 1800, {}, now=later)
        self.assertTrue(os.path.isdir(dirty) and os.path.isdir(ahead))
        self.assertEqual({branch for _, branch, _ in report["stale"]}, {"dirty", "ahead"})
        fresh = wh.run(self.repo, False, 1800, {})
        self.assertEqual(fresh["stale"], [])
        self.assertEqual({branch for _, branch in fresh["kept"]}, {"dirty", "ahead"})

    def test_a_merged_branch_without_a_worktree_is_deleted_and_an_unmerged_one_is_reported(self):
        _git(self.repo, "branch", "merged-old")
        _git(self.repo, "branch", "unmerged-old", "origin/main")
        _git(self.repo, "checkout", "-q", "unmerged-old")
        _commit(self.repo, "u")
        _git(self.repo, "checkout", "-q", "main")
        report = wh.run(self.repo, True, 1800, {}, now=time.time() + 3600)
        self.assertEqual(report["deleted"], ["merged-old"])
        self.assertEqual([branch for _, branch, _ in report["stale"]], ["unmerged-old"])
        self.assertIn("unmerged-old", _git(self.repo, "branch", "--format=%(refname:short)"))

    def test_the_exit_status_is_one_when_something_is_stale(self):
        path = self.worktree("ahead")
        with open(os.path.join(path, "old"), "w") as handle:
            handle.write("x")
        _git(path, "add", "old")
        env = dict(os.environ, GIT_COMMITTER_DATE="2020-01-01T00:00:00", GIT_AUTHOR_DATE="2020-01-01T00:00:00")
        subprocess.run(["git", "-C", path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "old"], env=env, check=True)
        self.assertEqual(wh.main([self.repo, "--no-fetch", "--stale-minutes", "30"]), 1)


if __name__ == "__main__":
    unittest.main()
