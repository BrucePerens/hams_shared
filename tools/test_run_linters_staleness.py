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


class TestFastForwardShared(unittest.TestCase):
    """The preflight fast-forwards a clean main checkout that is behind and refuses, in seconds, when it cannot."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.origin = os.path.join(self.tmp.name, "origin")
        os.makedirs(self.origin)
        git(self.origin, "init", "-q", "-b", "main")
        for n in ("a", "b", "c"):
            with open(os.path.join(self.origin, n), "w") as f:
                f.write(n)
            git(self.origin, "add", n)
            git(self.origin, "commit", "-q", "-m", n)
        self.clone = os.path.join(self.tmp.name, "clone")
        subprocess.run(["git", "clone", "-q", self.origin, self.clone], check=True, capture_output=True)
        git(self.clone, "checkout", "-q", "--detach", "HEAD~2")  # like the submodule checkout: detached, two behind

    def head(self, repo=None):
        return subprocess.run(["git", "-C", repo or self.clone, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    # Tests [@ANCHOR: run_linters:fast_forward_shared]
    def test_a_clean_checkout_behind_origin_is_fast_forwarded_and_stays_detached(self):
        ok, text = run_linters.fast_forward_shared(self.clone)
        self.assertTrue(ok, text)
        self.assertEqual(self.head(), self.head(self.origin))
        self.assertEqual(subprocess.run(["git", "-C", self.clone, "symbolic-ref", "-q", "HEAD"], capture_output=True).returncode, 1)

    # Tests [@ANCHOR: run_linters:fast_forward_shared]
    def test_modified_tracked_file_is_refused_and_left_untouched(self):
        with open(os.path.join(self.clone, "a"), "w") as f:
            f.write("my uncommitted work")
        before = self.head()
        ok, text = run_linters.fast_forward_shared(self.clone)
        self.assertFalse(ok)
        self.assertIn("uncommitted", text)
        self.assertEqual(self.head(), before)
        self.assertEqual(open(os.path.join(self.clone, "a")).read(), "my uncommitted work")

    # Tests [@ANCHOR: run_linters:fast_forward_shared]
    def test_untracked_files_do_not_block_it(self):
        with open(os.path.join(self.clone, "scratch.txt"), "w") as f:
            f.write("x")
        self.assertTrue(run_linters.fast_forward_shared(self.clone)[0])
        self.assertTrue(os.path.exists(os.path.join(self.clone, "scratch.txt")))

    # Tests [@ANCHOR: run_linters:fast_forward_shared]
    def test_a_diverged_checkout_is_refused_and_keeps_its_commit(self):
        with open(os.path.join(self.clone, "mine"), "w") as f:
            f.write("m")
        git(self.clone, "add", "mine")
        git(self.clone, "commit", "-q", "-m", "mine")
        before = self.head()
        ok, text = run_linters.fast_forward_shared(self.clone)
        self.assertFalse(ok)
        self.assertIn("not an ancestor", text)
        self.assertEqual(self.head(), before)

    # Tests [@ANCHOR: run_linters:ensure_shared_current]
    def test_ensure_reports_each_state(self):
        self.assertEqual(run_linters.ensure_shared_current(self.clone)[0], "fast_forwarded")
        self.assertEqual(run_linters.ensure_shared_current(self.clone), ("current", ""))
        git(self.clone, "checkout", "-q", "--detach", "HEAD~1")
        with open(os.path.join(self.clone, "a"), "w") as f:
            f.write("dirty")
        status, text = run_linters.ensure_shared_current(self.clone)
        self.assertEqual(status, "refused")
        self.assertIn("1 commit(s) behind", text)
        self.assertIn("merge --ff-only origin/main", text)
        work = os.path.join(self.tmp.name, "work")
        git(self.origin, "worktree", "add", "-q", "--detach", work, "HEAD~1")
        shared = os.path.join(self.tmp.name, "clone2")
        subprocess.run(["git", "clone", "-q", self.origin, shared], check=True, capture_output=True)
        git(shared, "worktree", "add", "-q", "--detach", os.path.join(self.tmp.name, "wt"), "HEAD~1")
        self.assertEqual(run_linters.ensure_shared_current(os.path.join(self.tmp.name, "wt"))[0], "worktree_behind")

    # Tests [@ANCHOR: run_linters:ensure_shared_current]
    def test_a_run_started_in_a_stale_clean_checkout_fast_forwards_itself_and_a_dirty_one_fails_fast(self):
        """End to end: the real script, copied into a throwaway repository, run with a deliberately stale checkout."""
        import time
        for dirty, want_exit in ((False, None), (True, 2)):
            repo = os.path.join(self.tmp.name, "e2e%d" % dirty)
            subprocess.run(["git", "clone", "-q", self.origin, repo], check=True, capture_output=True)
            git(repo, "checkout", "-q", "--detach", "HEAD~2")
            if dirty:
                with open(os.path.join(repo, "a"), "w") as f:
                    f.write("dirty")
            started = time.monotonic()
            code = ("import sys; sys.path.insert(0, %r); import run_linters; "
                    "s, t = run_linters.ensure_shared_current(%r); print(s); sys.exit(2 if s == 'refused' else 0)"
                    % (os.path.dirname(os.path.abspath(__file__)), repo))
            done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
            self.assertLess(time.monotonic() - started, 20)
            self.assertEqual(done.stdout.strip().splitlines()[-1], "refused" if dirty else "fast_forwarded", done.stderr)
            if want_exit:
                self.assertEqual(done.returncode, want_exit)


class TestRealSharedRoot(unittest.TestCase):
    # Tests [@ANCHOR: run_linters:real_shared_root]
    def test_through_a_repositorys_tools_link_it_is_hams_shared_not_the_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = os.path.realpath(os.path.join(tmp, "hams_shared"))
            os.makedirs(os.path.join(shared, "tools"))
            open(os.path.join(shared, "tools", "run_linters.py"), "w").close()
            repo = os.path.join(tmp, "hams_open")
            os.makedirs(repo)
            os.symlink(os.path.join(shared, "tools"), os.path.join(repo, "tools"))
            self.assertEqual(run_linters.real_shared_root(os.path.join(repo, "tools", "run_linters.py")), shared)

    # Tests [@ANCHOR: run_linters:real_shared_root]
    def test_started_in_hams_shared_itself_it_is_that_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = os.path.realpath(tmp)
            os.makedirs(os.path.join(shared, "tools"))
            open(os.path.join(shared, "tools", "run_linters.py"), "w").close()
            self.assertEqual(run_linters.real_shared_root(os.path.join(shared, "tools", "run_linters.py")), shared)


if __name__ == "__main__":
    unittest.main()
