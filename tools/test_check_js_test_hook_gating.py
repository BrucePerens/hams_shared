# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for which files check_js_test_hook_gating.py sends to the parser."""
import os
import subprocess
import tempfile
import unittest

import check_js_test_hook_gating as gating


class TrackedFileSelectionTests(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(dir=os.path.expanduser("~/.cache"))
        self.addCleanup(subprocess.run, ["rm", "-rf", self.repo], check=False)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)

    def _track(self, *rels):
        for rel in rels:
            path = os.path.join(self.repo, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("// x\n")
        subprocess.run(["git", "add", "-f", "."], cwd=self.repo, check=True)

    def _selected(self):
        return sorted(os.path.relpath(p, self.repo) for p in gating._git_tracked_js_files(self.repo))

    def test_production_javascript_is_checked(self):
        self._track("ham_shack/static/src/sw/shack_sw.js", "caching/static/src/sw.js")
        self.assertEqual(self._selected(), ["caching/static/src/sw.js", "ham_shack/static/src/sw/shack_sw.js"])

    def test_test_only_bundles_are_not_checked(self):
        self._track("ham_shack/static/tests/tours/shack_sw_behavior_tour.js", "ham_shack/static/src/sw/shack_sw.js")
        self.assertEqual(self._selected(), ["ham_shack/static/src/sw/shack_sw.js"])

    def test_a_directory_merely_named_like_tests_is_still_checked(self):
        self._track("static/tests_helpers/x.js", "mystatic/tests/y.js", "a/static/testsuite/z.js")
        self.assertEqual(self._selected(), ["a/static/testsuite/z.js", "mystatic/tests/y.js", "static/tests_helpers/x.js"])

    def test_vendored_minified_and_hoot_files_stay_excluded(self):
        self._track("m/static/lib/vendor.js", "m/static/src/a.min.js", "m/static/src/a.test.js", "m/static/src/ok.js")
        self.assertEqual(self._selected(), ["m/static/src/ok.js"])


class ScannerFailureTests(unittest.TestCase):
    """A node/acorn scan that did not run must fail the check, never pass it."""

    def _run_with(self, completed=None, error=None):
        def fake(*_args, **_kwargs):
            if error:
                raise error
            return completed

        original = gating.subprocess.run
        gating.subprocess.run = fake
        self.addCleanup(setattr, gating.subprocess, "run", original)
        return gating._run_js_scan(["/x/a.js"])

    def test_node_exit_failure_is_not_a_clean_scan(self):
        done = subprocess.CompletedProcess([], 1, stdout="", stderr="Cannot find module 'acorn'")
        with self.assertRaises(SystemExit):
            self._run_with(done)

    def test_non_json_output_is_not_a_clean_scan(self):
        with self.assertRaises(SystemExit):
            self._run_with(subprocess.CompletedProcess([], 0, stdout="oops", stderr=""))

    def test_missing_node_is_not_a_clean_scan(self):
        with self.assertRaises(SystemExit):
            self._run_with(error=FileNotFoundError("node"))

    def test_good_output_is_returned(self):
        out = '[{"file": "/x/a.js", "violations": [{"line": 3, "literal": "TEST_X"}]}]'
        got = self._run_with(subprocess.CompletedProcess([], 0, stdout=out, stderr=""))
        self.assertEqual(got, {"/x/a.js": [{"line": 3, "literal": "TEST_X"}]})


if __name__ == "__main__":
    unittest.main()
