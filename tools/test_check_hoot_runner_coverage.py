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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


if __name__ == "__main__":
    unittest.main()
