#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A tracked file with a non-ASCII name must be listed by the checkers that read `git ls-files`: without -z git quotes the
name (`"caf\\303\\251.py"`), which names no file, so the file was skipped and a finding in it was hidden.
[@ANCHOR: tree_files:git_listed]"""

import os
import subprocess
import tempfile
import unittest

import check_claims_freshness
import check_function_test_anchors
import check_js_function_test_anchors
import check_js_test_hook_gating
import check_rust_function_test_anchors
import gloss_first_use
from tree_files import git_listed

NAME = "café"


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], capture_output=True, check=True)


class NonAsciiNames(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = self._tmp.name
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "core.quotepath", "true")
        for rel in (f"m/{NAME}.py", f"m/{NAME}.js", f"m/{NAME}.rs", f"stories/{NAME}.md", f"m/{NAME}.claim", "plain.py"):
            path = os.path.join(self.repo, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("x\n")
        _git(self.repo, "add", "-A")

    def test_git_listed_decodes_the_name(self):
        self.assertIn(f"m/{NAME}.py", git_listed(self.repo, "ls-files", "*.py"))

    def test_python_anchor_checker_lists_it(self):
        self.assertIn(os.path.join(self.repo, "m", f"{NAME}.py"), check_function_test_anchors._git_tracked_python_files(self.repo))

    def test_js_anchor_checker_lists_it(self):
        self.assertIn(os.path.join(self.repo, "m", f"{NAME}.js"), check_js_function_test_anchors._git_tracked_js_files(self.repo))

    def test_js_hook_gating_lists_it(self):
        self.assertIn(os.path.join(self.repo, "m", f"{NAME}.js"), check_js_test_hook_gating._git_tracked_js_files(self.repo))

    def test_rust_anchor_checker_lists_it(self):
        self.assertIn(os.path.join(self.repo, "m", f"{NAME}.rs"), check_rust_function_test_anchors._git_tracked_rust_files(self.repo))

    def test_claims_freshness_lists_it(self):
        self.assertIn(os.path.join(self.repo, "m", f"{NAME}.claim"), check_claims_freshness._git_tracked_files(self.repo, ".claim"))

    def test_gloss_documents_lists_it(self):
        self.assertIn(os.path.join(self.repo, "stories", f"{NAME}.md"), gloss_first_use.default_documents(self.repo))


if __name__ == "__main__":
    unittest.main()
