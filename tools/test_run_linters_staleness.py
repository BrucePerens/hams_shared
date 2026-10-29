# Copyright © Bruce Perens K6BP. All Rights Reserved. This software is proprietary and confidential.
"""A hams_shared checkout that has fallen behind origin/main is detected by run_linters.py (see its docstring)."""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_linters  # noqa: E402


def git(cwd, *args):
    subprocess.run(["git", "-C", cwd, "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


class TestSharedCheckoutStaleness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.origin = os.path.join(self.tmp.name, "origin")
        os.makedirs(self.origin)
        git(self.origin, "init", "-q", "-b", "main")
        for n in ("a", "b"):
            with open(os.path.join(self.origin, n), "w") as f:
                f.write(n)
            git(self.origin, "add", n)
            git(self.origin, "commit", "-q", "-m", n)
        self.clone = os.path.join(self.tmp.name, "clone")
        subprocess.run(["git", "clone", "-q", self.origin, self.clone], check=True, capture_output=True)

    # Tests [@ANCHOR: run_linters:shared_checkout_staleness]
    def test_a_current_main_checkout_is_not_behind(self):
        self.assertEqual(run_linters.shared_checkout_staleness(self.clone), (0, True, None))

    # Tests [@ANCHOR: run_linters:shared_checkout_staleness]
    def test_a_main_checkout_behind_origin_reports_how_far(self):
        git(self.clone, "reset", "-q", "--hard", "HEAD~1")
        self.assertEqual(run_linters.shared_checkout_staleness(self.clone), (1, True, None))

    # Tests [@ANCHOR: run_linters:shared_checkout_staleness]
    def test_a_linked_worktree_that_is_behind_is_not_a_main_checkout(self):
        work = os.path.join(self.tmp.name, "work")
        git(self.clone, "worktree", "add", "-q", "--detach", work, "HEAD~1")
        self.assertEqual(run_linters.shared_checkout_staleness(work), (1, False, None))

    # Tests [@ANCHOR: run_linters:shared_checkout_staleness]
    def test_a_directory_without_origin_main_is_reported_not_guessed(self):
        bare = os.path.join(self.tmp.name, "plain")
        os.makedirs(bare)
        git(bare, "init", "-q", "-b", "main")
        behind, _is_main, error = run_linters.shared_checkout_staleness(bare)
        self.assertIsNone(behind)
        self.assertTrue(error)


if __name__ == "__main__":
    unittest.main()
