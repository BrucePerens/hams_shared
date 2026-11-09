#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
A moved submodule pointer (gitlink) is a code change in every changed-file computation of the lint tooling.

Found 2026-11-09 on hams_open pull request 1362, which only moved the `hams_shared` pointer: hams_open clones carry
`submodule.hams_shared.ignore=all`, a plain `git diff --name-only` hid the pointer, `prmerge.sh` printed "files changed: 0"
and "markdown-only change, exempt" and skipped the lint gate. Real throwaway repositories, a real submodule, the setting on.

Tests [@ANCHOR: lint_batch:changed_paths]
Tests [@ANCHOR: lint_stamp_gate:inherit_stamp]
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lint_batch  # noqa: E402
import lint_stamp_gate  # noqa: E402
import run_linters  # noqa: E402


class SubmoduleBumpFixture(unittest.TestCase):
    """`super`: branch `main` (the base) and branch `bump`: a work commit, then one commit that only moves the `hams_shared` pointer, with
    `submodule.hams_shared.ignore=all` set in the superproject's config."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="gitlink-")
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
        self.git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.git(self.repo, "checkout", "-q", "-b", "bump")
        with open(os.path.join(self.repo, "notes.txt"), "w", encoding="utf-8") as handle:
            handle.write("work\n")
        self.git(self.repo, "add", "notes.txt")
        self.git(self.repo, "commit", "-q", "-m", "work")
        self.stamped = self.git(self.repo, "rev-parse", "HEAD")
        self.git(os.path.join(self.repo, "hams_shared"), "checkout", "-q", "main")
        self.git(self.repo, "commit", "-q", "-am", "bump the pointer")
        self.tip = self.git(self.repo, "rev-parse", "HEAD")
        self.git(self.repo, "config", "submodule.hams_shared.ignore", "all")

    def git(self, cwd, *args):
        done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                               "-C", cwd, *args], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()


class GitlinkChangeTests(SubmoduleBumpFixture):
    def test_the_setting_really_hides_the_pointer_from_a_plain_diff(self):
        """The premise: without the fix's flag git reports nothing, so the tests below would pass vacuously if this failed."""
        self.assertEqual(self.git(self.repo, "diff", "--name-only", "origin/main...bump").split(), ["notes.txt"])
        self.assertEqual(self.git(self.repo, "diff", "--ignore-submodules=none", "--name-only", "origin/main...bump").split(),
                         ["hams_shared", "notes.txt"])

    def test_lint_batch_changed_paths_lists_the_moved_pointer(self):
        self.assertEqual(sorted(lint_batch.changed_paths(self.repo, "origin/main", self.tip)), ["hams_shared", "notes.txt"])

    def test_run_linters_changed_files_lists_the_moved_pointer(self):
        found = run_linters.changed_files(self.repo, "origin/main")
        top = os.path.realpath(self.repo)
        self.assertEqual(found, {os.path.join(top, "hams_shared"), os.path.join(top, "notes.txt")})

    def test_a_stamp_is_not_inherited_across_a_pointer_bump(self):
        """The stamp of the commit below the bump covered the old pointer: moving it is not a record-only change."""
        passed = os.path.join(self.root, "passed")
        os.makedirs(passed)
        with open(os.path.join(passed, self.stamped), "w", encoding="utf-8") as handle:
            handle.write("changed 3 flake8,check_cargo_clippy,check_cargo_fmt\n")
        messages = []
        self.assertIsNone(lint_stamp_gate.inherit_stamp(self.repo, self.tip, passed, base="origin/main", log=messages.append))
        self.assertFalse(os.path.exists(os.path.join(passed, self.tip)))
        self.assertTrue(any("hams_shared" in line for line in messages), messages)


if __name__ == "__main__":
    unittest.main()
