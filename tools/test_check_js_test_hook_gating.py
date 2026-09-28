# This software is distributed under the terms of the Affero General Public License (AGPL-3).
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


if __name__ == "__main__":
    unittest.main()
