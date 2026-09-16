#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_hoot_runner_coverage.py.
"""

import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

import check_hoot_runner_coverage as chk  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


_MANIFEST_WITH_TEST_JS = """{
    'name': 'fake_module',
    'assets': {
        'web.assets_unit_tests': [
            'fake_module/static/src/js/widget.js',
            'fake_module/static/tests/widget.test.js',
        ],
    },
}
"""

_MANIFEST_NO_TEST_JS = """{
    'name': 'fake_module',
    'assets': {
        'web.assets_unit_tests': [
            'fake_module/static/src/js/widget.js',
        ],
    },
}
"""

_HOOT_RUNNER_PY = """
class TestWidgetHoot(HamsHttpCase):
    def test_widget_hoot_suite_passes(self):
        self.browser_js(
            "/web/tests?headless&tag=fake_module_widget",
            "", "", login="admin", timeout=120,
            success_signal="[HOOT] Test suite succeeded",
        )
"""

_NON_HOOT_TEST_PY = """
class TestWidgetOther(HamsHttpCase):
    def test_something_else(self):
        pass
"""


class CheckHootRunnerCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                chk.main()
            except SystemExit as e:
                return e.code, buf.getvalue()
        return 0, buf.getvalue()

    def _run_with_argv(self, *roots):
        old_argv = sys.argv
        sys.argv = ["check_hoot_runner_coverage.py", *roots]
        try:
            return self._run()
        finally:
            sys.argv = old_argv

    def test_module_with_test_js_and_no_hoot_runner_is_flagged(self):
        mod = os.path.join(self.tmp, "fake_module")
        _write(os.path.join(mod, "__manifest__.py"), _MANIFEST_WITH_TEST_JS)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("fake_module", out)
        self.assertIn("widget.test.js", out)

    def test_module_with_test_js_and_a_hoot_runner_is_clean(self):
        mod = os.path.join(self.tmp, "fake_module")
        _write(os.path.join(mod, "__manifest__.py"), _MANIFEST_WITH_TEST_JS)
        _write(os.path.join(mod, "tests", "test_widget_hoot.py"), _HOOT_RUNNER_PY)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 0)
        self.assertNotIn("VIOLATION", out)

    def test_module_with_test_js_and_a_non_hoot_test_file_is_still_flagged(self):
        # A tests/test_*.py file that exists but never calls browser_js()
        # with a [HOOT] success_signal doesn't count -- it's not actually
        # running the hoot suite, just coincidentally present.
        mod = os.path.join(self.tmp, "fake_module")
        _write(os.path.join(mod, "__manifest__.py"), _MANIFEST_WITH_TEST_JS)
        _write(os.path.join(mod, "tests", "test_something_else.py"), _NON_HOOT_TEST_PY)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("fake_module", out)

    def test_module_with_no_test_js_at_all_is_never_flagged(self):
        mod = os.path.join(self.tmp, "fake_module")
        _write(os.path.join(mod, "__manifest__.py"), _MANIFEST_NO_TEST_JS)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 0)
        self.assertNotIn("VIOLATION", out)

    def test_burn_ignore_marker_in_manifest_suppresses_the_violation(self):
        mod = os.path.join(self.tmp, "fake_module")
        marked = _MANIFEST_WITH_TEST_JS.replace(
            "{\n", "{\n    # burn-ignore-hoot-runner-coverage: tracked follow-up\n", 1
        )
        _write(os.path.join(mod, "__manifest__.py"), marked)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 0)
        self.assertNotIn("VIOLATION", out)

    def test_a_module_without_any_manifest_is_skipped_without_crashing(self):
        os.makedirs(os.path.join(self.tmp, "not_a_module"), exist_ok=True)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 0)

    def test_multiple_roots_are_both_scanned(self):
        mod_a = os.path.join(self.tmp, "repo_a", "fake_module_a")
        mod_b = os.path.join(self.tmp, "repo_b", "fake_module_b")
        _write(os.path.join(mod_a, "__manifest__.py"), _MANIFEST_WITH_TEST_JS)
        _write(os.path.join(mod_b, "__manifest__.py"), _MANIFEST_WITH_TEST_JS)
        code, out = self._run_with_argv(
            os.path.join(self.tmp, "repo_a"), os.path.join(self.tmp, "repo_b")
        )
        self.assertEqual(code, 1)
        self.assertIn("fake_module_a", out)
        self.assertIn("fake_module_b", out)



class UnbundledHootSuiteTests(unittest.TestCase):
    """The same silent-pass failure as this file's other tests, in the other direction: a
    *.test.js on disk that no bundle names. /web/tests loads only what a manifest lists, so the
    wrapper's tag matches nothing, and hoot reports an empty run as "Test suite succeeded" --
    exactly what browser_js() waits for. Six real files were in this state on 2026-09-15.

    Deliberately NOT a subclass of CheckHootRunnerCoverageTests: inheriting it to reuse setUp and
    _run_with_argv would re-run all of its tests under this class's name too, inflating the
    reported count with duplicates. Overstating how much testing happened is the exact failure
    this whole check exists to catch, so it would be a poor thing to do in its own test file.
    """

    setUp = CheckHootRunnerCoverageTests.setUp
    tearDown = CheckHootRunnerCoverageTests.tearDown
    _run = CheckHootRunnerCoverageTests._run
    _run_with_argv = CheckHootRunnerCoverageTests._run_with_argv

    def test_a_test_js_on_disk_that_no_bundle_lists_is_flagged(self):
        mod = os.path.join(self.tmp, "fake_module")
        _write(os.path.join(mod, "__manifest__.py"), _MANIFEST_WITH_TEST_JS)
        _write(os.path.join(mod, "tests", "test_widget_hoot.py"), _HOOT_RUNNER_PY)
        _write(os.path.join(mod, "static", "tests", "orphan.test.js"), "// never bundled\n")
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("UNBUNDLED HOOT SUITE", out)
        self.assertIn("orphan.test.js", out)

    def test_a_bundled_test_js_on_disk_is_not_flagged(self):
        """The discriminating case: same file on disk, but named in the manifest."""
        mod = os.path.join(self.tmp, "fake_module")
        _write(os.path.join(mod, "__manifest__.py"), _MANIFEST_WITH_TEST_JS)
        _write(os.path.join(mod, "tests", "test_widget_hoot.py"), _HOOT_RUNNER_PY)
        _write(os.path.join(mod, "static", "tests", "widget.test.js"), "// bundled\n")
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 0)
        self.assertNotIn("UNBUNDLED", out)

    def test_a_tour_file_under_static_tests_tours_is_not_flagged(self):
        """Tours live in web.assets_tests and are driven by start_tour, not by a hoot tag, so
        they are not this check's business."""
        mod = os.path.join(self.tmp, "fake_module")
        _write(os.path.join(mod, "__manifest__.py"), _MANIFEST_WITH_TEST_JS)
        _write(os.path.join(mod, "tests", "test_widget_hoot.py"), _HOOT_RUNNER_PY)
        _write(os.path.join(mod, "static", "tests", "widget.test.js"), "// bundled\n")
        _write(os.path.join(mod, "static", "tests", "tours", "a_tour.test.js"), "// a tour\n")
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 0)
        self.assertNotIn("UNBUNDLED", out)

    def test_a_file_listed_in_some_other_bundle_is_not_flagged(self):
        """Wired up on purpose, even if arguably in the wrong bundle. Deciding which bundle is
        right needs the assets_unit_tests_setup reasoning ham_shack's manifest records at
        length; this check only catches a file mentioned nowhere at all."""
        mod = os.path.join(self.tmp, "fake_module")
        _write(
            os.path.join(mod, "__manifest__.py"),
            "{\n"
            "    'name': 'fake_module',\n"
            "    'assets': {\n"
            "        'web.assets_unit_tests': ['fake_module/static/tests/widget.test.js'],\n"
            "        'web.assets_tests': ['fake_module/static/tests/elsewhere.test.js'],\n"
            "    },\n"
            "}\n",
        )
        _write(os.path.join(mod, "tests", "test_widget_hoot.py"), _HOOT_RUNNER_PY)
        _write(os.path.join(mod, "static", "tests", "widget.test.js"), "// bundled\n")
        _write(os.path.join(mod, "static", "tests", "elsewhere.test.js"), "// other bundle\n")
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 0)
        self.assertNotIn("UNBUNDLED", out)

    def test_a_module_with_no_static_tests_directory_is_not_flagged(self):
        mod = os.path.join(self.tmp, "fake_module")
        _write(os.path.join(mod, "__manifest__.py"), _MANIFEST_NO_TEST_JS)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 0)
        self.assertNotIn("UNBUNDLED", out)


if __name__ == "__main__":
    unittest.main()
