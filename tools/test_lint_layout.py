#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for lint_layout.py: the sibling layout a hams_shared tree is linted in.

Real git repositories in a temporary directory stand in for the three repositories: a hams_open whose
`hams_shared` is a gitlink to a nested repository (as the real submodule is), a hams_com whose tracked
`hams_shared` link points into hams_open, and a worktree of the nested hams_shared under lint. The real
`verify_anchors.py` then judges the trees, so the tests show the incident of 2026-11-07 itself: a hams_shared change
whose documentation cites an anchor that exists nowhere fails when linted in the layout, while the scan the old
code made (the main checkouts, whose submodule is the old commit) passed it.

Tests [@ANCHOR: lint_layout:Layout]
Tests [@ANCHOR: lint_layout:base_commit]
Tests [@ANCHOR: lint_layout:build]
Tests [@ANCHOR: lint_layout:remove]
Tests [@ANCHOR: lint_layout:mirror_changed]
Tests [@ANCHOR: lint_layout:reap_orphans]
Tests [@ANCHOR: run_linters:stamp_repos]
"""

import os
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import lint_layout  # noqa: E402
import run_linters  # noqa: E402

VERIFY_ANCHORS = os.path.join(_HERE, "verify_anchors.py")
GIT_ENV = dict(
    os.environ,
    GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@localhost", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@localhost",
)


def git(repo, *args):
    result = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=GIT_ENV)
    assert result.returncode == 0, "git %s: %s" % (" ".join(args), result.stderr)
    return result.stdout.strip()


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def commit_all(repo, message):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


class Workspace:
    """Three repositories under one temporary directory, plus a hams_shared worktree `shared_tree`."""

    def __init__(self, root):
        self.root = root
        self.open_main = os.path.join(root, "hams_open")
        self.com_main = os.path.join(root, "hams_com")
        self.shared_main = os.path.join(self.open_main, "hams_shared")
        self.shared_tree = os.path.join(root, "work", "shared_branch")
        self.environ = {"HAMS_BATCH_TREE_DIR": os.path.join(root, "trees")}
        os.makedirs(self.shared_main)
        git(self.shared_main, "init", "-q", "-b", "main")
        write(self.shared_main, "docs/readme.md", "documentation\n")
        write(self.shared_main, "tools/helper.py", "VALUE = 1\n")
        write(self.shared_main, "eslint.config.js", "module.exports = [];\n")
        commit_all(self.shared_main, "shared")
        os.makedirs(self.open_main, exist_ok=True)
        git(self.open_main, "init", "-q", "-b", "main")
        write(self.open_main, "m/__manifest__.py", "{'name': 'm'}\n")
        write(self.open_main, "m/__init__.py", "")
        for name in ("agents", "AGENTS.md", "docs", "tools"):
            os.symlink("hams_shared/" + name, os.path.join(self.open_main, name))
        # `git add` of a directory that holds a repository records a gitlink, as the real submodule is.
        git(self.open_main, "add", "-A")
        git(self.open_main, "commit", "-q", "-m", "open")
        os.makedirs(self.com_main)
        git(self.com_main, "init", "-q", "-b", "main")
        write(self.com_main, "c/__manifest__.py", "{'name': 'c'}\n")
        write(self.com_main, "c/__init__.py", "")
        os.symlink("../hams_open/hams_shared", os.path.join(self.com_main, "hams_shared"))
        commit_all(self.com_main, "com")
        os.makedirs(os.path.dirname(self.shared_tree), exist_ok=True)
        git(self.shared_main, "worktree", "add", "-q", "-b", "branch", self.shared_tree)

    def build(self, **options):
        options.setdefault("fetch", False)
        options.setdefault("out", lambda _text: None)
        return lint_layout.build(self.shared_tree, self.open_main, self.com_main, environ=self.environ, **options)


def verify_anchors(*roots):
    """(exit code, output) of the real verify_anchors.py over `roots`, as run_linters.py calls it."""
    result = subprocess.run([sys.executable, VERIFY_ANCHORS, *roots], capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


def verify_in_layout(layout):
    return verify_anchors(layout.hams_open, layout.hams_com)


class LayoutTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="lint-layout-test-")
        self.addCleanup(tmp.cleanup)
        self.ws = Workspace(os.path.realpath(tmp.name))


class BuildTests(LayoutTestCase):
    """Tests [@ANCHOR: lint_layout:build] [@ANCHOR: lint_layout:Layout] [@ANCHOR: lint_layout:remove]"""

    def test_the_layout_holds_this_hams_shared_inside_a_sibling_hams_open(self):
        write(self.ws.shared_tree, "docs/new.md", "new\n")
        commit_all(self.ws.shared_tree, "branch change")
        layout = self.ws.build()
        self.addCleanup(lint_layout.remove, layout)
        self.assertEqual(os.path.dirname(layout.hams_open), os.path.dirname(layout.hams_com))
        self.assertTrue(os.path.isfile(os.path.join(layout.hams_open, "hams_shared", "docs", "new.md")))
        self.assertFalse(os.path.islink(os.path.join(layout.hams_open, "hams_shared")))
        self.assertTrue(os.path.isfile(os.path.join(layout.hams_open, "tools", "helper.py")), "the tools link resolves")
        self.assertTrue(os.path.isfile(os.path.join(layout.hams_com, "hams_shared", "docs", "new.md")), "hams_com's link too")
        self.assertEqual(layout.shared_copy, os.path.join(layout.hams_open, "hams_shared"))
        self.assertEqual(layout.shared_source, os.path.realpath(self.ws.shared_tree))

    def test_uncommitted_files_are_in_the_layout_and_deleted_ones_are_gone(self):
        write(self.ws.shared_tree, "docs/uncommitted.md", "u\n")
        write(self.ws.shared_tree, "tools/helper.py", "VALUE = 2\n")
        os.remove(os.path.join(self.ws.shared_tree, "docs", "readme.md"))
        layout = self.ws.build()
        self.addCleanup(lint_layout.remove, layout)
        copy = layout.shared_copy
        self.assertTrue(os.path.isfile(os.path.join(copy, "docs", "uncommitted.md")))
        with open(os.path.join(copy, "tools", "helper.py"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "VALUE = 2\n")
        self.assertFalse(os.path.exists(os.path.join(copy, "docs", "readme.md")))

    def test_the_main_checkouts_and_the_worktree_are_not_changed(self):
        write(self.ws.shared_tree, "docs/new.md", "new\n")
        commit_all(self.ws.shared_tree, "branch change")
        before = [git(r, "status", "--porcelain") for r in (self.ws.open_main, self.ws.com_main, self.ws.shared_tree)]
        layout = self.ws.build()
        lint_layout.remove(layout)
        after = [git(r, "status", "--porcelain") for r in (self.ws.open_main, self.ws.com_main, self.ws.shared_tree)]
        self.assertEqual(before, after)
        self.assertEqual(git(self.ws.shared_main, "worktree", "list").count("\n"), 1, "no worktree was registered")

    def test_remove_deletes_the_directory_and_is_safe_twice(self):
        layout = self.ws.build()
        self.assertTrue(os.path.isdir(layout.parent))
        lint_layout.remove(layout)
        self.assertFalse(os.path.exists(layout.parent))
        lint_layout.remove(layout)

    def test_a_missing_hams_com_gives_a_layout_without_it(self):
        layout = lint_layout.build(self.ws.shared_tree, self.ws.open_main, os.path.join(self.ws.root, "nowhere"),
                                   environ=self.ws.environ, fetch=False, out=lambda _text: None)
        self.addCleanup(lint_layout.remove, layout)
        self.assertIsNone(layout.hams_com)

    def test_a_failure_raises_and_leaves_nothing_behind(self):
        with self.assertRaises(lint_layout.LayoutError):
            lint_layout.build(self.ws.shared_tree, os.path.join(self.ws.root, "not_a_repo"), self.ws.com_main,
                              environ=self.ws.environ, fetch=False, out=lambda _text: None)
        trees = self.ws.environ["HAMS_BATCH_TREE_DIR"]
        self.assertEqual(os.listdir(trees) if os.path.isdir(trees) else [], [])


class IncidentTests(LayoutTestCase):
    """The 2026-11-07 incident: verify_anchors judges the branch's hams_shared, not the main checkouts' old one.
    Tests [@ANCHOR: lint_layout:build]"""

    CITING_DOC = "The runbook cites [@ANCHOR: lint_layout_fix:cited] in a form the scanner reports.\n"

    def commit_citing_doc(self):
        write(self.ws.shared_tree, "docs/runbooks/RUNBOOK.md", self.CITING_DOC)
        commit_all(self.ws.shared_tree, "docs cite an anchor that exists nowhere")

    def test_docs_citing_a_missing_anchor_fail_the_branch_gate(self):
        self.commit_citing_doc()
        layout = self.ws.build()
        self.addCleanup(lint_layout.remove, layout)
        code, output = verify_in_layout(layout)
        self.assertNotEqual(code, 0, output)
        self.assertIn("lint_layout_fix:cited", output)

    def test_the_main_checkouts_do_not_see_the_branch_which_is_the_old_bug(self):
        self.commit_citing_doc()
        code, output = verify_anchors(self.ws.open_main, self.ws.com_main)
        self.assertEqual(code, 0, "the main checkouts' hams_shared is the old commit, so they cannot see the branch\n" + output)
        self.assertNotIn("lint_layout_fix", output)

    def test_with_the_citation_fixed_the_branch_passes(self):
        """The fix #412 made: the runbook names the entry without the bracketed anchor form (a docs/ citation of a
        tools/ anchor is reported as missing from source)."""
        self.commit_citing_doc()
        write(self.ws.shared_tree, "docs/runbooks/RUNBOOK.md", "The runbook names the lint_layout_fix cited entry in words.\n")
        commit_all(self.ws.shared_tree, "fix: no bracketed citation")
        layout = self.ws.build()
        self.addCleanup(lint_layout.remove, layout)
        code, output = verify_in_layout(layout)
        self.assertEqual(code, 0, output)

    def test_an_uncommitted_citing_doc_also_fails(self):
        write(self.ws.shared_tree, "docs/runbooks/RUNBOOK.md", self.CITING_DOC)
        layout = self.ws.build()
        self.addCleanup(lint_layout.remove, layout)
        code, output = verify_in_layout(layout)
        self.assertNotEqual(code, 0, output)


class BaseCommitTests(LayoutTestCase):
    """Tests [@ANCHOR: lint_layout:base_commit]"""

    def test_origin_main_wins_over_head_and_head_is_the_fallback(self):
        head = git(self.ws.com_main, "rev-parse", "HEAD")
        self.assertEqual(lint_layout.base_commit(self.ws.com_main, fetch=False), head, "no origin: HEAD")
        write(self.ws.com_main, "c/more.py", "X = 1\n")
        commit_all(self.ws.com_main, "second")
        newer = git(self.ws.com_main, "rev-parse", "HEAD")
        git(self.ws.com_main, "update-ref", "refs/remotes/origin/main", head)
        git(self.ws.com_main, "reset", "-q", "--hard", newer)
        self.assertEqual(lint_layout.base_commit(self.ws.com_main, fetch=False), head, "origin/main is preferred")

    def test_a_directory_that_is_not_a_repository_gives_none(self):
        with tempfile.TemporaryDirectory() as plain:
            self.assertIsNone(lint_layout.base_commit(plain, fetch=False))


class MirrorChangedTests(LayoutTestCase):
    """Tests [@ANCHOR: lint_layout:mirror_changed]"""

    def test_paths_below_the_shared_tree_gain_their_copy_and_others_are_kept(self):
        layout = self.ws.build()
        self.addCleanup(lint_layout.remove, layout)
        inside = os.path.join(layout.shared_source, "docs", "a.md")
        outside = os.path.join(self.ws.root, "elsewhere.txt")
        original = {inside, outside}
        result = lint_layout.mirror_changed(original, layout)
        self.assertEqual(result, {inside, outside, os.path.join(layout.shared_copy, "docs", "a.md")})
        self.assertEqual(original, {inside, outside}, "the argument is not modified")

    def test_without_a_layout_the_set_is_returned_as_it_is(self):
        changed = {"/x/y"}
        self.assertIs(lint_layout.mirror_changed(changed, None), changed)


class ReapOrphansTests(LayoutTestCase):
    """Tests [@ANCHOR: lint_layout:reap_orphans]"""

    def test_a_layout_of_a_dead_process_is_removed_and_a_live_one_is_kept(self):
        trees = self.ws.environ["HAMS_BATCH_TREE_DIR"]
        dead = os.path.join(trees, "batch_lintlayout_hams_shared_999999-1")
        os.makedirs(os.path.join(dead, "hams_open"))
        live = self.ws.build()
        self.addCleanup(lint_layout.remove, live)
        removed = lint_layout.reap_orphans(self.ws.open_main, environ=self.ws.environ)
        self.assertEqual(removed, [dead])
        self.assertFalse(os.path.exists(dead))
        self.assertTrue(os.path.isdir(live.parent))


class StampReposTests(LayoutTestCase):
    """Tests [@ANCHOR: run_linters:stamp_repos]"""

    def test_a_layout_run_stamps_only_the_hams_shared_tree(self):
        layout = self.ws.build()
        self.addCleanup(lint_layout.remove, layout)
        tools_dir = os.path.join(self.ws.shared_tree, "tools")
        repos = run_linters.stamp_repos(layout.hams_open, tools_dir, self.ws.shared_tree, layout)
        self.assertEqual([os.path.realpath(r) for r in repos], [os.path.realpath(self.ws.shared_tree)])


if __name__ == "__main__":
    unittest.main()

# Class anchors (ADR 0113) exercised by the tests above.
# Tests [@ANCHOR: lint_layout:lint_layout_layout_error_class]
