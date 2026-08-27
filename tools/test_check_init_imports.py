#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_init_imports.py.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_init_imports as chk  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_init_imports.py")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class GetImportedNamesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _init(self, content):
        p = os.path.join(self.tmp, "__init__.py")
        _write(p, content)
        return p

    def test_from_dot_import_module(self):
        p = self._init("from . import ham_qso\n")
        self.assertEqual(chk.get_imported_names(p), {"ham_qso"})

    def test_from_dot_module_import_name(self):
        p = self._init("from .ham_qso import HamQSO\n")
        self.assertEqual(chk.get_imported_names(p), {"ham_qso"})

    def test_bare_import_statement(self):
        p = self._init("import ham_qso\n")
        self.assertEqual(chk.get_imported_names(p), {"ham_qso"})

    def test_a_level_two_relative_import_is_not_counted(self):
        # node.level == 1 is the guard -- `from .. import x` (level 2)
        # reaches a different package's __init__.py, not this one's own
        # sibling modules, so it must not satisfy this directory's own
        # import-completeness check.
        p = self._init("from .. import something_else\n")
        self.assertEqual(chk.get_imported_names(p), set())

    def test_multiple_imports_across_statements(self):
        p = self._init(
            "from . import ham_qso\n"
            "from .ham_award import HamAward\n"
            "import third_party\n"
        )
        self.assertEqual(chk.get_imported_names(p), {"ham_qso", "ham_award", "third_party"})

    def test_a_syntax_error_is_reported_and_returns_an_empty_set(self):
        p = self._init("from . import (: broken")
        self.assertEqual(chk.get_imported_names(p), set())

    def test_a_missing_file_returns_an_empty_set_without_crashing(self):
        self.assertEqual(
            chk.get_imported_names(os.path.join(self.tmp, "does_not_exist.py")), set()
        )


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

    def test_a_module_correctly_imported_by_its_init_passes(self):
        _write(os.path.join(self.tmp, "mod_a", "models", "__init__.py"), "from . import ham_qso\n")
        _write(os.path.join(self.tmp, "mod_a", "models", "ham_qso.py"), "class HamQSO:\n    pass\n")
        code, out = self._run()
        self.assertEqual(code, 0, out)

    def test_a_module_never_imported_by_its_init_is_flagged(self):
        _write(os.path.join(self.tmp, "mod_a", "models", "__init__.py"), "\n")
        _write(os.path.join(self.tmp, "mod_a", "models", "ham_qso.py"), "class HamQSO:\n    pass\n")
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("never imported", out)

    def test_manifest_and_setup_files_are_never_flagged_even_if_unimported(self):
        _write(os.path.join(self.tmp, "mod_a", "__init__.py"), "\n")
        _write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        _write(os.path.join(self.tmp, "mod_a", "setup.py"), "\n")
        code, out = self._run()
        self.assertEqual(code, 0, out)

    def test_a_directory_with_no_init_py_is_never_scanned(self):
        _write(os.path.join(self.tmp, "mod_a", "not_a_package", "orphan.py"), "\n")
        code, out = self._run()
        self.assertEqual(code, 0, out)

    def test_an_explicitly_excluded_file_is_never_flagged(self):
        _write(os.path.join(self.tmp, "ham_shack", "tests", "__init__.py"), "\n")
        _write(
            os.path.join(self.tmp, "ham_shack", "tests", "verify_noise_xx_handshake.py"),
            "# real-browser Playwright script, not an Odoo test\n",
        )
        code, out = self._run()
        self.assertEqual(code, 0, out)

    def test_excluded_subdirectories_are_never_walked(self):
        _write(os.path.join(self.tmp, "daemons", "some_daemon", "__init__.py"), "\n")
        _write(os.path.join(self.tmp, "daemons", "some_daemon", "orphan.py"), "\n")
        code, out = self._run()
        self.assertEqual(code, 0, out)

    def test_generated_odoo_type_stubs_tree_is_never_walked(self):
        # odoo_type_stubs/ is machine-generated by generate_odoo_core_stubs.py and wiped/rebuilt
        # on every run -- it deliberately mirrors real core-addon file paths rather than being a
        # normal importable module, and mypy reads these stubs by path, not via Python imports.
        # Regression test for a real false-positive this check produced against the generated
        # tree (604 files flagged) before this exclusion existed.
        _write(
            os.path.join(
                self.tmp,
                "hams_shared",
                "tools",
                "odoo_type_stubs",
                "odoo",
                "addons",
                "account",
                "models",
                "__init__.py",
            ),
            "\n",
        )
        _write(
            os.path.join(
                self.tmp,
                "hams_shared",
                "tools",
                "odoo_type_stubs",
                "odoo",
                "addons",
                "account",
                "models",
                "account_account.py",
            ),
            "class AccountAccount:\n    pass\n",
        )
        code, out = self._run()
        self.assertEqual(code, 0, out)

    def test_an_unrelated_dir_named_odoo_type_stubs_is_not_exempted(self):
        # The exclusion matches the exact generated path (hams_shared/tools/odoo_type_stubs), not
        # just the bare directory name -- a directory that happens to share that name somewhere
        # else in the repo must still be checked normally.
        _write(
            os.path.join(self.tmp, "some_module", "odoo_type_stubs", "__init__.py"), "\n"
        )
        _write(
            os.path.join(self.tmp, "some_module", "odoo_type_stubs", "orphan.py"), "\n"
        )
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("never imported", out)


if __name__ == "__main__":
    unittest.main()
