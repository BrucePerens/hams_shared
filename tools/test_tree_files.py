# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import shutil
import tempfile
import unittest

import tree_files_testkit as kit
from tree_files import tree_files


class TreeFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def rels(self, found):
        return [os.path.relpath(p, self.tmp) for p in found]

    def test_a_nested_foreign_worktree_is_not_scanned(self):
        kit.checkout_with_nested_worktree(self.tmp, "a/mine.py", "x = 1\n")
        self.assertEqual(self.rels(tree_files(self.tmp, ".py")), [os.path.join("a", "mine.py")])

    def test_the_nested_worktree_scans_its_own_files_when_it_is_the_root(self):
        kit.checkout_with_nested_worktree(self.tmp, "a/mine.py", "x = 1\n")
        other = os.path.join(self.tmp, "tmp", "other")
        self.assertEqual([os.path.relpath(p, other) for p in tree_files(other, ".py")], [os.path.join("a", "mine.py")])

    def test_in_git_untracked_files_are_not_scanned(self):
        kit.checkout_with_nested_worktree(self.tmp, "a/mine.py", "x = 1\n")
        kit.write(self.tmp, "scratch/untracked.py", "x = 1\n")
        self.assertEqual(self.rels(tree_files(self.tmp, ".py")), [os.path.join("a", "mine.py")])

    def test_in_git_skip_dirs_and_hidden_dirs_and_accept_still_apply(self):
        kit.git(self.tmp, "init", "-q")
        for rel in ("a/keep.py", "a/target/skip.py", ".hid/skip.py", "b/test_x.py", "b/other.py", "a/keep.txt"):
            kit.write(self.tmp, rel, "x = 1\n")
        kit.git(self.tmp, "add", "-f", ".")
        got = self.rels(tree_files(self.tmp, ".py", {"target"}, skip_hidden=True, accept=lambda r: "test_" not in r))
        self.assertEqual(got, [os.path.join("a", "keep.py"), os.path.join("b", "other.py")])
        got = self.rels(tree_files(self.tmp, ".py"))
        self.assertIn(os.path.join(".hid", "skip.py"), got, "hidden directories are entered unless asked not to")

    def test_in_git_submodule_files_are_included(self):
        sub = os.path.join(self.tmp, "sub")
        os.makedirs(sub)
        kit.git(sub, "init", "-q")
        kit.write(sub, "lib/in_sub.py", "x = 1\n")
        kit.git(sub, "add", ".")
        kit.git(sub, "commit", "-q", "-m", "x")
        top = os.path.join(self.tmp, "top")
        os.makedirs(top)
        kit.git(top, "init", "-q")
        kit.git(top, "-c", "protocol.file.allow=always", "submodule", "add", "-q", sub, "dep")
        kit.write(top, "mine.py", "x = 1\n")
        kit.git(top, "add", ".")
        got = [os.path.relpath(p, top) for p in tree_files(top, ".py")]
        self.assertEqual(got, [os.path.join("dep", "lib", "in_sub.py"), "mine.py"])

    def test_outside_git_a_directory_with_its_own_dot_git_is_not_entered(self):
        kit.write(self.tmp, "a/mine.py", "x = 1\n")
        kit.write(self.tmp, "nested/.git", "gitdir: /nowhere\n")
        kit.write(self.tmp, "nested/theirs.py", "x = 1\n")
        self.assertEqual(self.rels(tree_files(self.tmp, ".py")), [os.path.join("a", "mine.py")])

    def test_outside_git_suffix_tuple_skip_dirs_and_hidden_dirs(self):
        for rel in ("a/x.rs", "a/y.py", "a/z.txt", "node_modules/q.rs", ".hid/h.rs"):
            kit.write(self.tmp, rel, "\n")
        got = self.rels(tree_files(self.tmp, (".rs", ".py"), {"node_modules"}, skip_hidden=True))
        self.assertEqual(got, [os.path.join("a", "x.rs"), os.path.join("a", "y.py")])


if __name__ == "__main__":
    unittest.main()
