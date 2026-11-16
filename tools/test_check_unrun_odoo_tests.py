#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for check_unrun_odoo_tests.py (Odoo tests that no runner executes)."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import check_unrun_odoo_tests as chk  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_unrun_odoo_tests.py")

_ODOO_CASE = "from odoo.tests.common import BaseCase, tagged\n\n@tagged('post_install', '-at_install')\nclass TestA(BaseCase):\n    def test_x(self):\n        pass\n"
_PLAIN_CASE = "import unittest\n\nclass TestPlain(unittest.TestCase):\n    def test_x(self):\n        pass\n"


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


class UnrunTestsTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        _write(os.path.join(self.root, "mod", "__manifest__.py"), "{}")
        self.tests = os.path.join(self.root, "mod", "tests")

    def run_checker(self):
        return subprocess.run([sys.executable, _SCRIPT, self.root], capture_output=True, text=True, check=False)

    def test_odoo_case_imported_passes(self):
        _write(os.path.join(self.tests, "__init__.py"), "from . import test_a\n")
        _write(os.path.join(self.tests, "test_a.py"), _ODOO_CASE)
        done = self.run_checker()
        self.assertEqual(done.returncode, 0, done.stdout)

    def test_plain_unittest_class_fails(self):
        _write(os.path.join(self.tests, "__init__.py"), "from . import test_a\n")
        _write(os.path.join(self.tests, "test_a.py"), _PLAIN_CASE)
        done = self.run_checker()
        self.assertEqual(done.returncode, 1)
        self.assertIn("TestPlain", done.stdout)

    def test_bare_testcase_import_and_no_base_fail(self):
        source = "from unittest import TestCase\n\nclass TestB(TestCase):\n    def test_x(self):\n        pass\n\nclass TestC:\n    def test_y(self):\n        pass\n"
        self.assertEqual([name for _line, name in chk.plain_test_classes(chk.ast.parse(source))], ["TestB", "TestC"])

    def test_unittest_plus_unknown_mixin_still_fails_but_odoo_case_base_passes(self):
        source = (
            "import unittest\nfrom x import SafePatchMixin, HamsTransactionCase\n\n"
            "class TestA(unittest.TestCase, SafePatchMixin):\n    def test_x(self):\n        pass\n\n"
            "class TestB(HamsTransactionCase):\n    def test_x(self):\n        pass\n"
        )
        self.assertEqual([name for _line, name in chk.plain_test_classes(chk.ast.parse(source))], ["TestA"])

    def test_mixin_used_as_base_and_helper_are_fine(self):
        source = (
            "from odoo.tests.common import BaseCase\n\n"
            "class _Checks:\n    def test_x(self):\n        pass\n\n"
            "class Shared:\n    def test_y(self):\n        pass\n\n"
            "class TestA(Shared, BaseCase):\n    pass\n"
        )
        self.assertEqual(chk.plain_test_classes(chk.ast.parse(source)), [])

    def test_file_not_imported_fails(self):
        _write(os.path.join(self.tests, "__init__.py"), "from . import test_a\n")
        _write(os.path.join(self.tests, "test_a.py"), _ODOO_CASE)
        _write(os.path.join(self.tests, "test_b.py"), _ODOO_CASE)
        done = self.run_checker()
        self.assertEqual(done.returncode, 1)
        self.assertIn("test_b.py: not imported", done.stdout)
        self.assertNotIn("test_a.py: not imported", done.stdout)

    def test_subpackage_import_chain_is_followed(self):
        _write(os.path.join(self.tests, "__init__.py"), "from . import sub\nfrom .other import something\n")
        _write(os.path.join(self.tests, "sub", "__init__.py"), "from . import test_deep\n")
        _write(os.path.join(self.tests, "sub", "test_deep.py"), _ODOO_CASE)
        _write(os.path.join(self.tests, "other.py"), "something = 1\n")
        done = self.run_checker()
        self.assertEqual(done.returncode, 0, done.stdout)


if __name__ == "__main__":
    unittest.main()
