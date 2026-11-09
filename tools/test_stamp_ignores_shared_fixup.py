#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
A pass stamp is written when the only difference in the work tree is the known state of the `hams_shared` submodule path.

Found 2026-11-09: a hams_open worktree showed ` M hams_shared` (its submodule checkout was moved to origin/main, which the
preflight demands, while the commit records an older pointer), so `run_linters.py` found the tree dirty and wrote no stamp. An agent
worked around it with `git config submodule.hams_shared.ignore all` in the shared config, which hides a moved pointer from every plain
`git status` in every worktree. `tree_status_lines` ignores exactly that unstaged submodule state for this one call. Real throwaway
repositories and a real submodule; no setting in any config file.

Tests [@ANCHOR: run_linters:tree_status_lines]
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_linters  # noqa: E402


class TreeStatusTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="stampfix-")
        self.addCleanup(shutil.rmtree, self.root, True)
        sub = os.path.join(self.root, "sub")
        self.repo = os.path.join(self.root, "super")
        os.mkdir(sub)
        self.git(sub, "init", "-q", "-b", "main")
        self.git(sub, "commit", "-q", "--allow-empty", "-m", "s1")
        self.git(sub, "commit", "-q", "--allow-empty", "-m", "s2")
        os.mkdir(self.repo)
        self.git(self.repo, "init", "-q", "-b", "main")
        self.git(self.repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", sub, "hams_shared")
        self.git(os.path.join(self.repo, "hams_shared"), "checkout", "-q", "HEAD~1")
        self.git(self.repo, "commit", "-q", "-am", "base")
        os.environ["XDG_CACHE_HOME"] = os.path.join(self.root, "cache")
        self.addCleanup(os.environ.pop, "XDG_CACHE_HOME", None)

    def git(self, cwd, *args):
        done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                               "-C", cwd, *args], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def _stamped(self):
        head = self.git(self.repo, "rev-parse", "HEAD")
        return os.path.exists(os.path.join(run_linters.pass_stamp_dir(), head))

    def _move_submodule_checkout(self):
        self.git(os.path.join(self.repo, "hams_shared"), "checkout", "-q", "main")

    def _replace_submodule_with_symlink(self, skip_worktree):
        """What fix_worktree_symlinks.py does: the submodule directory becomes a symlink to another checkout."""
        shutil.rmtree(os.path.join(self.repo, "hams_shared"))
        os.symlink(os.path.join(self.root, "sub"), os.path.join(self.repo, "hams_shared"))
        if skip_worktree:
            self.git(self.repo, "update-index", "--skip-worktree", "hams_shared")

    def test_premise_a_moved_checkout_is_modified_for_plain_git_status(self):
        self._move_submodule_checkout()
        plain = self.git(self.repo, "-c", "submodule.hams_shared.ignore=none", "status", "--porcelain")
        self.assertEqual(plain, "M hams_shared")

    def test_a_moved_submodule_checkout_is_not_dirt_and_the_stamp_is_written(self):
        self._move_submodule_checkout()
        self.assertEqual(run_linters.tree_status_lines(self.repo), [])
        run_linters._write_pass_stamps([self.repo], "changed")
        self.assertTrue(self._stamped())

    def test_the_symlink_fixup_is_not_dirt(self):
        self._replace_submodule_with_symlink(skip_worktree=True)
        self.assertEqual(run_linters.tree_status_lines(self.repo), [])
        run_linters._write_pass_stamps([self.repo], "changed")
        self.assertTrue(self._stamped())

    def test_the_symlink_without_skip_worktree_makes_plain_status_fail(self):
        """Why the stamp was not written even before any pointer moved: git refuses, and a failed status reads as dirty."""
        self._replace_submodule_with_symlink(skip_worktree=False)
        done = subprocess.run(["git", "-C", self.repo, "-c", "submodule.hams_shared.ignore=none", "status", "--porcelain"],
                              capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(run_linters.tree_status_lines(self.repo), [])

    def test_a_staged_pointer_change_is_still_dirt(self):
        self._move_submodule_checkout()
        self.git(self.repo, "add", "hams_shared")
        self.assertEqual(run_linters.tree_status_lines(self.repo), ["M  hams_shared"])
        run_linters._write_pass_stamps([self.repo], "changed")
        self.assertFalse(self._stamped())

    def test_any_other_change_is_still_dirt_beside_the_submodule_state(self):
        self._move_submodule_checkout()
        with open(os.path.join(self.repo, "new.txt"), "w", encoding="utf-8") as handle:
            handle.write("x\n")
        self.assertEqual(run_linters.tree_status_lines(self.repo), ["?? new.txt"])
        run_linters._write_pass_stamps([self.repo], "changed")
        self.assertFalse(self._stamped())

    def test_the_setting_is_not_left_in_the_repository_config(self):
        run_linters.tree_status_lines(self.repo)
        done = subprocess.run(["git", "-C", self.repo, "config", "--get", "submodule.hams_shared.ignore"], capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)

    def test_a_directory_that_is_not_a_repository_is_none(self):
        self.assertIsNone(run_linters.tree_status_lines(self.root + "/missing"))


if __name__ == "__main__":
    unittest.main()
