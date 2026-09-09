#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_window_fetch_reassignment.py.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_window_fetch_reassignment as chk  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class CheckWindowFetchReassignmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_direct_dot_assignment_is_flagged(self):
        _write(
            os.path.join(self.tmp, "foo.test.js"),
            "QUnit.test('x', async () => {\n    window.fetch = () => Promise.resolve({});\n});\n",
        )
        violations = chk.check_window_fetch_reassignment(self.tmp)
        self.assertEqual(len(violations), 1)
        self.assertIn("foo.test.js:2", violations[0])

    def test_bracket_string_assignment_is_flagged(self):
        _write(
            os.path.join(self.tmp, "foo.test.js"),
            "window['fetch'] = mockImpl;\n",
        )
        violations = chk.check_window_fetch_reassignment(self.tmp)
        self.assertEqual(len(violations), 1)
        self.assertIn("foo.test.js:1", violations[0])

    def test_globalthis_dot_assignment_is_flagged(self):
        # globalThis.fetch resolves to the same hoot-mocked, read-only
        # window.fetch, so it throws the exact same way -- must be caught
        # by the same rule, not just window.fetch.
        _write(
            os.path.join(self.tmp, "foo.test.js"),
            "globalThis.fetch = () => Promise.resolve({});\n",
        )
        violations = chk.check_window_fetch_reassignment(self.tmp)
        self.assertEqual(len(violations), 1)
        self.assertIn("foo.test.js:1", violations[0])

    def test_the_sanctioned_mockfetch_helper_is_not_flagged(self):
        _write(
            os.path.join(self.tmp, "foo.test.js"),
            "import { mockFetch } from '@web/../tests/helpers/mock_services';\n"
            "mockFetch((route) => ({}));\n",
        )
        self.assertEqual(chk.check_window_fetch_reassignment(self.tmp), [])

    def test_an_equality_comparison_is_not_a_reassignment(self):
        # Real, non-obvious distinction the regex must get right: reading
        # window.fetch (e.g. asserting it's still the original function)
        # is legitimate and must not be flagged as an assignment.
        _write(
            os.path.join(self.tmp, "foo.test.js"),
            "expect(window.fetch === originalFetch).toBe(true);\n",
        )
        self.assertEqual(chk.check_window_fetch_reassignment(self.tmp), [])

    def test_a_non_test_js_file_is_never_scanned(self):
        _write(
            os.path.join(self.tmp, "foo.js"),
            "window.fetch = () => Promise.resolve({});\n",
        )
        self.assertEqual(chk.check_window_fetch_reassignment(self.tmp), [])

    def test_a_clean_test_file_produces_no_violations(self):
        _write(
            os.path.join(self.tmp, "foo.test.js"),
            "QUnit.test('x', async () => { mockFetch(() => ({})); });\n",
        )
        self.assertEqual(chk.check_window_fetch_reassignment(self.tmp), [])

    def test_an_ignored_directory_is_never_walked(self):
        _write(
            os.path.join(self.tmp, "node_modules", "pkg", "bad.test.js"),
            "window.fetch = x;\n",
        )
        self.assertEqual(chk.check_window_fetch_reassignment(self.tmp), [])

    def test_a_stale_claude_worktree_checkout_is_never_walked(self):
        # Found live wiring this check into run_linters.py: frozen,
        # historical .claude/worktrees/<hash>/ copies of already-fixed
        # test.js files still had the old, broken pattern, which used to
        # get flagged even though the real source was clean.
        _write(
            os.path.join(self.tmp, ".claude", "worktrees", "agent-abc123", "foo.test.js"),
            "window.fetch = x;\n",
        )
        self.assertEqual(chk.check_window_fetch_reassignment(self.tmp), [])

    def test_a_binary_file_with_invalid_utf8_is_skipped_without_crashing(self):
        path = os.path.join(self.tmp, "bad.test.js")
        os.makedirs(self.tmp, exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"\xff\xfe\x00window.fetch = 1;")
        # Must not raise.
        chk.check_window_fetch_reassignment(self.tmp)

    def test_multiple_violations_across_files_are_all_reported(self):
        _write(os.path.join(self.tmp, "a.test.js"), "window.fetch = a;\n")
        _write(os.path.join(self.tmp, "b.test.js"), "window.fetch = b;\n")
        violations = chk.check_window_fetch_reassignment(self.tmp)
        self.assertEqual(len(violations), 2)


if __name__ == "__main__":
    unittest.main()
