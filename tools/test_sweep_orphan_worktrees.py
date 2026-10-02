#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for sweep_orphan_worktrees.py.

Hermetic: builds real, throwaway git repositories and real `git worktree add` checkouts under
a tempdir (never the real hams_com/hams_open trees, and cleaned up in tearDown), rather than
mocking git. Written 2026-09-29 after finding this tool had no test coverage at all -- the fix
that motivated writing these (tolerating fix_worktree_symlinks.py's own known, harmless drift)
is exactly the kind of change that should never ship without a real negative test proving it
doesn't also mask genuine dirt. The fixture below deliberately mirrors the real repo layout
(a symlink whose committed *relative* target only resolves correctly from the main checkout's
own top level, and resolves to the wrong place from several directories deeper inside a
worktree) rather than a same-directory toy case, because a same-directory case does not
exercise the actual bug this tool exists to tolerate.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

import sweep_orphan_worktrees as sow  # noqa: E402


def _run(args, cwd):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout


def _init_repo(path):
    os.makedirs(path, exist_ok=True)
    _run(["git", "init", "--initial-branch=main", "-q"], cwd=path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=path)
    _run(["git", "config", "user.name", "Test"], cwd=path)


class SymlinkLocalizationDriftTests(unittest.TestCase):
    """`_is_known_symlink_localization_drift` / `_is_clean_and_pushed`'s tolerance for it.

    Mirrors the real hams_com/hams_shared layout: a sibling checkout (`sibling/shared_dir`)
    outside the repo, and a repo-root symlink to it with committed content `../sibling/shared_dir`
    -- correct when resolved from the repo's own top level, wrong when resolved from three
    directories deeper inside a worktree (`.claude/worktrees/<name>/`).
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sow_test_")
        self.sibling_target = os.path.join(self.tmp, "sibling", "shared_dir")
        os.makedirs(self.sibling_target, exist_ok=True)
        with open(os.path.join(self.sibling_target, "AGENTS.md"), "w") as f:
            f.write("real shared content\n")

        self.origin = os.path.join(self.tmp, "origin")
        _init_repo(self.origin)
        # A relative symlink to the sibling checkout, exactly the real hams_shared pattern.
        os.symlink("../sibling/shared_dir", os.path.join(self.origin, "hams_shared"))
        with open(os.path.join(self.origin, "README.md"), "w") as f:
            f.write("hi\n")
        _run(["git", "add", "-A"], cwd=self.origin)
        _run(["git", "commit", "-q", "-m", "initial"], cwd=self.origin)

        self.repo = os.path.join(self.tmp, "repo")
        _run(["git", "clone", "-q", self.origin, self.repo], cwd=self.tmp)
        self.worktrees_dir = os.path.join(self.repo, ".claude", "worktrees")
        os.makedirs(self.worktrees_dir, exist_ok=True)
        self.worktree = os.path.join(self.worktrees_dir, "sample")
        _run(["git", "worktree", "add", "-q", "-b", "sample-branch", self.worktree], cwd=self.repo)
        _run(["git", "push", "-q", "-u", "origin", "sample-branch"], cwd=self.worktree)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_genuinely_clean_pushed_worktree_is_clean(self):
        self.assertTrue(sow._is_clean_and_pushed(self.repo, self.worktree))

    def test_the_naive_checkout_of_the_committed_relative_symlink_resolves_wrong(self):
        """Sanity check on the fixture itself: matches the real bug this tool exists for."""
        link = os.path.join(self.worktree, "hams_shared")
        naive = os.path.normpath(os.path.join(os.path.dirname(link), os.readlink(link)))
        self.assertNotEqual(naive, self.sibling_target)

    def test_the_known_symlink_localization_drift_still_counts_as_clean(self):
        """The exact real-world case this fix targets: the worktree's own symlink has been
        re-pointed to an absolute path that IS the correct real target -- different string,
        same real location the committed relative target means at the repo's own top level."""
        link = os.path.join(self.worktree, "hams_shared")
        os.remove(link)
        os.symlink(self.sibling_target, link)
        self.assertTrue(sow._is_clean_and_pushed(self.repo, self.worktree))

    def test_a_symlink_repointed_somewhere_genuinely_different_is_not_clean(self):
        """A real, accidental (or malicious) content change must never be waved through just
        because the changed path happens to be a symlink."""
        link = os.path.join(self.worktree, "hams_shared")
        os.remove(link)
        other = os.path.join(self.tmp, "not_the_real_target")
        os.makedirs(other, exist_ok=True)
        os.symlink(other, link)
        self.assertFalse(sow._is_clean_and_pushed(self.repo, self.worktree))

    def test_an_ordinary_file_edit_is_not_clean(self):
        """A ' M' change to a plain (non-symlink) tracked file must never be waved through."""
        with open(os.path.join(self.worktree, "README.md"), "a") as f:
            f.write("an extra real line\n")
        self.assertFalse(sow._is_clean_and_pushed(self.repo, self.worktree))

    def test_an_untracked_file_alongside_the_known_drift_is_not_clean(self):
        """A brand-new untracked file (real, unfinished work) must not be masked just because
        the worktree also happens to carry the known symlink drift."""
        link = os.path.join(self.worktree, "hams_shared")
        os.remove(link)
        os.symlink(self.sibling_target, link)
        with open(os.path.join(self.worktree, "scratch_notes.md"), "w") as f:
            f.write("real unfinished work\n")
        self.assertFalse(sow._is_clean_and_pushed(self.repo, self.worktree))

    def test_an_unpushed_commit_is_not_clean(self):
        with open(os.path.join(self.worktree, "new_file.txt"), "w") as f:
            f.write("x\n")
        _run(["git", "add", "-A"], cwd=self.worktree)
        _run(["git", "commit", "-q", "-m", "unpushed"], cwd=self.worktree)
        self.assertFalse(sow._is_clean_and_pushed(self.repo, self.worktree))


class RealSymlinkTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sow_test_")
        self.real_dir = os.path.join(self.tmp, "real_dir")
        os.makedirs(self.real_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_relative_target_resolves_against_the_anchor_directory(self):
        anchor = os.path.join(self.tmp, "anchor")
        os.makedirs(anchor)
        self.assertEqual(
            sow._real_symlink_target(anchor, "../real_dir"),
            self.real_dir,
        )

    def test_an_absolute_target_is_used_as_is(self):
        self.assertEqual(sow._real_symlink_target("/irrelevant", self.real_dir), self.real_dir)

    def test_a_path_routed_through_an_intermediate_symlink_resolves_to_the_same_real_location(self):
        """The actual crux of the fix: a target reached by walking through another symlink
        compares equal to the same real location written out directly, in full."""
        indirect_root = os.path.join(self.tmp, "indirect_root")
        os.makedirs(indirect_root)
        os.symlink(self.real_dir, os.path.join(indirect_root, "via_symlink"))
        via_indirection = sow._real_symlink_target(indirect_root, "via_symlink")
        direct = sow._real_symlink_target("/irrelevant", self.real_dir)
        self.assertEqual(via_indirection, direct)


class SweepRepoEndToEndTests(unittest.TestCase):
    """`sweep_repo` itself, not just the helpers it now uses."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sow_test_")
        self.sibling_target = os.path.join(self.tmp, "sibling", "shared_dir")
        os.makedirs(self.sibling_target, exist_ok=True)
        with open(os.path.join(self.sibling_target, "AGENTS.md"), "w") as f:
            f.write("real shared content\n")

        self.origin = os.path.join(self.tmp, "origin")
        _init_repo(self.origin)
        os.symlink("../sibling/shared_dir", os.path.join(self.origin, "hams_shared"))
        with open(os.path.join(self.origin, "README.md"), "w") as f:
            f.write("hi\n")
        _run(["git", "add", "-A"], cwd=self.origin)
        _run(["git", "commit", "-q", "-m", "initial"], cwd=self.origin)
        self.repo = os.path.join(self.tmp, "repo")
        _run(["git", "clone", "-q", self.origin, self.repo], cwd=self.tmp)
        os.makedirs(os.path.join(self.repo, ".claude", "worktrees"), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add_worktree(self, name):
        path = os.path.join(self.repo, ".claude", "worktrees", name)
        _run(["git", "worktree", "add", "-q", "-b", f"{name}-branch", path], cwd=self.repo)
        return path

    def _age(self, path, hours):
        """Back-date every file's mtime so `_idle_hours` sees it as old enough to sweep."""
        stamp = __import__("time").time() - hours * 3600 - 60
        for dirpath, dirnames, filenames in os.walk(path):
            if ".git" in dirnames:
                dirnames.remove(".git")
            for name in filenames:
                # A never-yet-localized hams_shared-style symlink can be genuinely dangling
                # (matches real pre-fix worktrees); _idle_hours already tolerates that with its
                # own try/except, so this just needs to not crash on the same case.
                try:
                    os.utime(os.path.join(dirpath, name), (stamp, stamp))
                except FileNotFoundError:
                    continue

    def test_a_clean_pushed_idle_worktree_is_removed(self):
        path = self._add_worktree("done")
        _run(["git", "push", "-q", "-u", "origin", "done-branch"], cwd=path)
        self._age(path, sow._MIN_IDLE_HOURS + 1)
        sow.sweep_repo(self.repo)
        self.assertFalse(os.path.isdir(path))

    def test_a_worktree_with_only_the_known_drift_is_removed(self):
        path = self._add_worktree("drifted")
        _run(["git", "push", "-q", "-u", "origin", "drifted-branch"], cwd=path)
        link = os.path.join(path, "hams_shared")
        os.remove(link)
        os.symlink(self.sibling_target, link)
        self._age(path, sow._MIN_IDLE_HOURS + 1)

        sow.sweep_repo(self.repo)
        self.assertFalse(os.path.isdir(path))

    def test_a_worktree_with_real_uncommitted_work_is_flagged_not_removed(self):
        path = self._add_worktree("wip")
        _run(["git", "push", "-q", "-u", "origin", "wip-branch"], cwd=path)
        with open(os.path.join(path, "unfinished.md"), "w") as f:
            f.write("real work in progress\n")
        self._age(path, sow._MIN_IDLE_HOURS + 1)

        sow.sweep_repo(self.repo)
        self.assertTrue(os.path.isdir(path), "real uncommitted work must never be deleted")
        todo_dir = os.path.join(self.repo, "night_shift_todo", "low")
        flagged = [n for n in os.listdir(todo_dir) if "wip" in n] if os.path.isdir(todo_dir) else []
        self.assertTrue(flagged, "a worktree with real dirt must be flagged, not silently skipped")

    def test_a_too_recently_touched_worktree_is_left_alone_entirely(self):
        path = self._add_worktree("fresh")
        sow.sweep_repo(self.repo)
        self.assertTrue(os.path.isdir(path))
        todo_dir = os.path.join(self.repo, "night_shift_todo", "low")
        if os.path.isdir(todo_dir):
            self.assertEqual(os.listdir(todo_dir), [])


if __name__ == "__main__":
    unittest.main()
