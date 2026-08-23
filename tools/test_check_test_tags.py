#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_test_tags.py (Odoo test tag enforcer).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_test_tags as chk  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_test_tags.py")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class CheckTestFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _path(self, content):
        p = os.path.join(self.tmp, "test_something.py")
        _write(p, content)
        return p

    def test_a_properly_tagged_class_passes(self):
        p = self._path(
            "@tagged('post_install', '-at_install')\n"
            "class TestFoo(TransactionCase):\n"
            "    pass\n"
        )
        self.assertTrue(chk.check_test_file(p))

    def test_a_class_with_no_tagged_decorator_at_all_is_flagged(self):
        p = self._path("class TestFoo(TransactionCase):\n    pass\n")
        self.assertFalse(chk.check_test_file(p))

    def test_tagged_missing_at_install_negation_is_flagged(self):
        p = self._path(
            "@tagged('post_install')\n"
            "class TestFoo(TransactionCase):\n"
            "    pass\n"
        )
        self.assertFalse(chk.check_test_file(p))

    def test_tagged_missing_post_install_is_flagged(self):
        p = self._path(
            "@tagged('-at_install')\n"
            "class TestFoo(TransactionCase):\n"
            "    pass\n"
        )
        self.assertFalse(chk.check_test_file(p))

    def test_extra_tags_alongside_the_required_pair_still_pass(self):
        p = self._path(
            "@tagged('post_install', '-at_install', 'my_module')\n"
            "class TestFoo(TransactionCase):\n"
            "    pass\n"
        )
        self.assertTrue(chk.check_test_file(p))

    def test_a_class_not_named_test_something_is_never_checked(self):
        p = self._path("class HelperMixin(object):\n    pass\n")
        self.assertTrue(chk.check_test_file(p))

    def test_the_bypass_comment_exempts_the_whole_file(self):
        p = self._path(
            "# burn-ignore-test-tags\n"
            "class TestFoo(TransactionCase):\n"
            "    pass\n"
        )
        self.assertTrue(chk.check_test_file(p))

    def test_a_syntax_error_is_reported_and_returns_false_without_crashing(self):
        p = self._path("class TestFoo(: broken syntax")
        self.assertFalse(chk.check_test_file(p))

    def test_multiple_classes_only_the_untagged_one_fails_the_file(self):
        p = self._path(
            "@tagged('post_install', '-at_install')\n"
            "class TestGood(TransactionCase):\n"
            "    pass\n\n"
            "class TestBad(TransactionCase):\n"
            "    pass\n"
        )
        self.assertFalse(chk.check_test_file(p))

    def test_a_decorator_that_is_not_a_call_at_all_is_not_mistaken_for_tagged(self):
        # e.g. a bare `@tagged` with no parens (a real, if unusual, Python
        # decorator form) must not be treated as satisfying the check
        # just because a decorator named "tagged" is present.
        p = self._path("@tagged\nclass TestFoo(TransactionCase):\n    pass\n")
        self.assertFalse(chk.check_test_file(p))


class MainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        result = subprocess.run(
            [sys.executable, _SCRIPT, self.tmp], capture_output=True, text=True, timeout=30
        )
        return result.returncode, result.stdout + result.stderr

    def test_a_clean_tests_directory_passes(self):
        _write(
            os.path.join(self.tmp, "mod_a", "tests", "test_foo.py"),
            "@tagged('post_install', '-at_install')\n"
            "class TestFoo(TransactionCase):\n"
            "    pass\n",
        )
        code, out = self._run()
        self.assertEqual(code, 0, out)

    def test_an_untagged_class_in_a_tests_directory_fails(self):
        _write(
            os.path.join(self.tmp, "mod_a", "tests", "test_foo.py"),
            "class TestFoo(TransactionCase):\n    pass\n",
        )
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("TEST TAGGING VIOLATION", out)

    def test_a_file_outside_any_tests_directory_is_never_scanned(self):
        # Same untagged class shape, but not under a "tests" directory --
        # not this checker's concern (e.g. a helper module that happens to
        # define a Test-prefixed class outside the tests/ convention).
        _write(
            os.path.join(self.tmp, "mod_a", "models", "test_helper.py"),
            "class TestFoo(TransactionCase):\n    pass\n",
        )
        code, out = self._run()
        self.assertEqual(code, 0, out)

    def test_a_non_test_prefixed_file_inside_tests_is_not_scanned(self):
        _write(
            os.path.join(self.tmp, "mod_a", "tests", "helpers.py"),
            "class TestFoo(TransactionCase):\n    pass\n",
        )
        code, out = self._run()
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
