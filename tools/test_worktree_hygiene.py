#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for worktree_hygiene.py against real temporary git repositories (a bare origin, a clone, real worktrees)."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

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

    def test_a_branch_made_now_at_an_old_commit_is_not_deleted_as_idle(self):
        env = {**os.environ, "GIT_COMMITTER_DATE": "2020-01-01T00:00:00", "GIT_AUTHOR_DATE": "2020-01-01T00:00:00"}
        with open(os.path.join(self.repo, "old"), "w") as handle:
            handle.write("x")
        _git(self.repo, "add", "old")
        subprocess.run(
            ["git", "-C", self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "old"], env=env, check=True
        )
        _git(self.repo, "push", "-q", "origin", "main")
        _git(self.repo, "branch", "at-old-commit", "origin/main")
        report = wh.run(self.repo, True, 1800, {})
        self.assertEqual(report["deleted"], [])
        self.assertIn("at-old-commit", _git(self.repo, "branch", "--format=%(refname:short)"))

    def test_the_main_working_trees_own_branch_is_not_deleted_and_does_not_abort_the_run(self):
        _git(self.repo, "checkout", "-q", "-b", "on-main-tree", "origin/main")
        _git(self.repo, "branch", "merged-old")
        report = wh.run(self.repo, True, 1800, {}, now=time.time() + 3600)
        self.assertEqual(report["deleted"], ["merged-old"])
        self.assertIn("on-main-tree", _git(self.repo, "branch", "--format=%(refname:short)"))

    def test_a_missing_gh_gives_no_merged_heads_instead_of_an_exception(self):
        with unittest.mock.patch.object(wh.subprocess, "run", side_effect=FileNotFoundError("gh")):
            self.assertEqual(wh.merged_pull_request_heads(self.repo), {})

    def test_the_exit_status_is_one_when_something_is_stale(self):
        path = self.worktree("ahead")
        with open(os.path.join(path, "old"), "w") as handle:
            handle.write("x")
        _git(path, "add", "old")
        env = dict(os.environ, GIT_COMMITTER_DATE="2020-01-01T00:00:00", GIT_AUTHOR_DATE="2020-01-01T00:00:00")
        subprocess.run(["git", "-C", path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "old"], env=env, check=True)
        self.assertEqual(wh.main([self.repo, "--no-fetch", "--stale-minutes", "30"]), 1)

    def _hold(self, path, how):
        """A real running process holding `path`: by its working directory, or by an open file inside it."""
        if how == "cwd":
            proc = subprocess.Popen(["sleep", "60"], cwd=path)
        else:
            with open(os.path.join(path, "a")) as handle:  # `a` is tracked, so the tree stays clean
                proc = subprocess.Popen(["sleep", "60"], stdin=handle, cwd="/")
        self.addCleanup(proc.wait)
        self.addCleanup(proc.terminate)
        return proc

    def test_a_worktree_held_by_a_running_process_is_kept_until_the_process_ends(self):
        for how in ("cwd", "file"):
            with self.subTest(how=how):
                path = self.worktree(f"held-{how}")
                proc = self._hold(path, how)
                report = wh.run(self.repo, True, 1800, {}, now=time.time() + 3600)
                self.assertTrue(os.path.isdir(path), report)
                self.assertEqual(report["removed"], [])
                self.assertEqual([(p, b, [pid for pid, _ in who]) for p, b, who in report["held"]], [(path, f"held-{how}", [proc.pid])])
                self.assertIn(f"held-{how}", _git(self.repo, "branch", "--format=%(refname:short)"))
                proc.terminate()
                proc.wait()
                report = wh.run(self.repo, True, 1800, {}, now=time.time() + 3600)
                self.assertEqual(report["removed"], [(path, f"held-{how}")])
                self.assertFalse(os.path.isdir(path))

    def test_the_callers_own_shell_chain_does_not_count_as_holding(self):
        self.assertIn(os.getpid(), wh.calling_shells())
        path = self.worktree("mine")
        proc = self._hold(path, "cwd")
        self.assertEqual([pid for pid, _ in wh.holding_processes(path, exempt=set())], [proc.pid])
        self.assertEqual(wh.holding_processes(path, exempt={proc.pid}), [])

    def test_a_zero_idle_limit_is_met_even_when_a_file_time_is_ahead_of_the_clock(self):
        path = self.worktree("ahead-clock")
        future = time.time() + 5
        os.utime(os.path.join(path, "a"), (future, future))
        os.utime(_git(path, "rev-parse", "--absolute-git-dir") + "/HEAD", (future, future))
        report = wh.run(self.repo, True, 0, {}, only_branch="ahead-clock")
        self.assertEqual(report["removed"], [(path, "ahead-clock")])

    def test_a_locked_worktree_is_never_removed(self):
        path = self.worktree("locked")
        _git(self.repo, "worktree", "lock", "--reason", "batch run", path)
        report = wh.run(self.repo, True, 1800, {}, now=time.time() + 3600)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual([p for p, _, _ in report["held"]], [path])

    def test_the_holders_mode_lists_what_holds_a_path(self):
        path = self.worktree("lister")
        proc = self._hold(path, "cwd")
        out = subprocess.run([sys.executable, wh.__file__, "--holders", path], capture_output=True, text=True, check=True).stdout
        self.assertIn(f"{proc.pid} sleep", out.splitlines())

    def _post_merge(self, name, tip, cwd):
        """`prmerge.sh`'s call: from a shell sitting in `cwd`, after the pull request merged `tip`."""
        script = f"cd {cwd} && {sys.executable} {wh.__file__} --apply --no-fetch --branch {name} --tip {tip} {self.repo}"
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    def test_post_merge_cleanup_run_from_the_merged_worktree_removes_it_and_its_branch(self):
        path = self.worktree("squashed", commits=2)
        other = self.worktree("other-merged")
        os.symlink(self.tmp.name, os.path.join(path, "link_outside"))  # like hams_shared -> the main checkout's
        subprocess.run(["git", "-C", path, "add", "link_outside"], check=True)
        _git(path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "link")
        tip = _git(path, "rev-parse", "HEAD")
        result = self._post_merge("squashed", tip, path)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("MERGED   removed worktree", result.stdout)
        self.assertFalse(os.path.isdir(path))
        self.assertTrue(os.path.isdir(self.tmp.name), "a symlink in the worktree was followed")
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "a")))
        self.assertNotIn("squashed", _git(self.repo, "branch", "--format=%(refname:short)"))
        self.assertTrue(os.path.isdir(other), "only the named branch is cleaned")
        self.assertEqual(_git(self.repo, "branch", "--show-current"), "main")

    def test_post_merge_cleanup_keeps_dirty_unmerged_and_held_worktrees_and_says_why(self):
        dirty = self.worktree("dirty", commits=1, dirty=True)
        extra = self.worktree("extra", commits=1)
        merged_tip = _git(extra, "rev-parse", "HEAD")
        _commit(extra, "after_the_merge")  # a commit made after the pull request's head was merged
        held = self.worktree("held", commits=1)
        held_tip = _git(held, "rev-parse", "HEAD")
        proc = self._hold(held, "cwd")
        for name, path, tip, word in (
            ("dirty", dirty, _git(dirty, "rev-parse", "HEAD"), "STALE"),
            ("extra", extra, merged_tip, "STALE"),
            ("held", held, held_tip, "HELD"),
        ):
            result = self._post_merge(name, tip, "/")
            self.assertTrue(os.path.isdir(path), (name, result.stdout, result.stderr))
            self.assertIn(word, result.stdout, name)
            self.assertIn(name, _git(self.repo, "branch", "--format=%(refname:short)"))
        self.assertIn(f"sleep (pid {proc.pid})", result.stdout)

    def test_post_merge_cleanup_deletes_a_merged_branch_that_has_no_worktree(self):
        _git(self.repo, "branch", "gone", "origin/main")
        result = self._post_merge("gone", _git(self.repo, "rev-parse", "origin/main"), "/")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("gone", _git(self.repo, "branch", "--format=%(refname:short)"))

    def nested(self, name, dirty=False):
        """A worktree inside the main checkout (the layout that made scans of that checkout read it)."""
        path = os.path.join(self.repo, "tmp", name)
        _git(self.repo, "worktree", "add", "-q", "-b", name, path, "origin/main")
        if dirty:
            with open(os.path.join(path, "scratch"), "w") as handle:
                handle.write("unsaved")
        return path

    # Tests [@ANCHOR: global:worktree_hygiene:container_checkout]
    def test_container_checkout_finds_the_checkout_a_worktree_lies_in(self):
        self.assertEqual(wh.container_checkout(self.nested("n1")), self.repo)
        self.assertIsNone(wh.container_checkout(self.worktree("clean")))

    # Tests [@ANCHOR: global:worktree_hygiene:nested_worktrees]
    def test_nested_worktrees_are_flagged_and_dirty_or_held_ones_say_why(self):
        free, dirty, held, clean = self.nested("free"), self.nested("dirty", dirty=True), self.nested("held"), self.worktree("clean")
        found = {p: why for p, _b, _c, why in wh.nested_worktrees(self.repo, holders_of=lambda p: [(7, "vim")] if p == held else [])}
        self.assertEqual(found, {free: None, dirty: "uncommitted changes", held: "in use by vim (pid 7)"})
        self.assertNotIn(clean, found)

    def test_the_main_run_exits_one_for_a_nested_worktree_and_zero_without(self):
        self.assertEqual(wh.main([self.repo, "--no-fetch"]), 0)
        self.nested("n2")
        self.assertEqual(wh.main([self.repo, "--no-fetch"]), 1)

    # Tests [@ANCHOR: global:worktree_hygiene:move_nested]
    def test_move_nested_moves_a_clean_worktree_to_a_job_directory_with_sibling_links(self):
        ws = os.path.join(self.tmp.name, "ws")
        os.makedirs(os.path.join(ws, "tmp"))
        os.makedirs(os.path.join(ws, "hams_open"))
        main = os.path.join(ws, "hams_com")
        subprocess.run(["git", "clone", "-q", os.path.join(self.tmp.name, "origin.git"), main], check=True, capture_output=True)
        _git(main, "checkout", "-qB", "main")
        path = os.path.join(main, "tmp", "topic")
        _git(main, "worktree", "add", "-q", "-b", "topic", path, "origin/main")
        new = wh.move_nested(main, path, workspace=ws)
        self.assertEqual(new, os.path.join(ws, "tmp", "moved-topic", "hams_com"))
        self.assertTrue(os.path.isfile(os.path.join(new, "a")))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(sorted(os.listdir(os.path.dirname(new))), ["hams_com", "hams_open"])
        self.assertEqual([p for p, _b in wh.worktrees(main)], [new])
        other = os.path.join(main, "tmp", "topic2")
        _git(main, "worktree", "add", "-q", "-b", "topic2", other, "origin/main")
        os.makedirs(os.path.join(ws, "tmp", "moved-topic2"))
        with self.assertRaises(RuntimeError):
            wh.move_nested(main, other, workspace=ws)  # the target job directory exists
        self.assertTrue(os.path.isdir(other))

    def test_move_nested_mode_never_moves_dirty_work(self):
        dirty = self.nested("dirty2", dirty=True)
        with unittest.mock.patch.object(wh, "move_nested") as mover:
            self.assertEqual(wh.main([self.repo, "--no-fetch", "--apply", "--move-nested"]), 1)
        mover.assert_not_called()
        self.assertTrue(os.path.isfile(os.path.join(dirty, "scratch")))

    # Tests [@ANCHOR: global:worktree_hygiene:registered_jobs]
    # Tests [@ANCHOR: global:worktree_hygiene:reap_finished_jobs]
    def test_a_finished_registered_job_directory_is_removed_only_when_it_holds_just_links(self):
        state = os.path.join(self.tmp.name, "state")
        os.makedirs(state)
        gone, mixed, live = (os.path.join(self.tmp.name, n) for n in ("gone", "mixed", "live"))
        for job_dir in (gone, mixed, live):
            os.makedirs(job_dir)
            os.symlink(self.repo, os.path.join(job_dir, "hams_open"))
        with open(os.path.join(mixed, "notes.txt"), "w") as handle:
            handle.write("keep me")
        os.makedirs(os.path.join(live, "hams_com"))
        for name, job_dir in (("gone", gone), ("mixed", mixed), ("live", live)):
            with open(os.path.join(state, name + ".json"), "w") as handle:
                json.dump({"job": name, "job_dir": job_dir, "worktree": os.path.join(job_dir, "hams_com")}, handle)
        with open(os.path.join(state, "bad.json"), "w") as handle:
            handle.write("{")
        self.assertEqual(len(wh.registered_jobs(state)), 3)
        self.assertEqual(sorted(wh.reap_finished_jobs(False, state)), sorted([(gone, True), (mixed, False)]))
        self.assertTrue(os.path.isdir(gone))
        wh.reap_finished_jobs(True, state)
        self.assertFalse(os.path.lexists(gone))
        self.assertFalse(os.path.exists(os.path.join(state, "gone.json")))
        self.assertTrue(os.path.isfile(os.path.join(mixed, "notes.txt")))
        self.assertTrue(os.path.isdir(live))
        self.assertTrue(os.path.isdir(self.repo))  # the link targets are untouched


if __name__ == "__main__":
    unittest.main()
