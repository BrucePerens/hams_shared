#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_module_subpackage_imports.py.

Regression test for the real bug that motivated this checker: ham_propagation/__init__.py only
imported `controllers`, so a new `ham_propagation/models/` subpackage (a real _inherit extension
of ham.sked) was silently never loaded by Odoo at all -- the new method existed as correct
Python on disk but Odoo's own view validation reported "action_suggest_band is not a valid
action on ham.sked", since nothing had ever imported the file defining it.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_module_subpackage_imports as chk  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class CheckModuleTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def test_a_models_subpackage_never_imported_is_flagged(self):
        # Regression test for the real ham_propagation bug this checker exists to catch.
        module_dir = os.path.join(self.tmpdir, "test_mod")
        _write(os.path.join(module_dir, "__init__.py"), "from . import controllers\n")
        _write(os.path.join(module_dir, "models", "__init__.py"), "from . import thing\n")
        errors = chk._check_module("test_mod", module_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("models", errors[0])
        self.assertIn("silently dead code", errors[0])

    def test_a_models_subpackage_imported_via_from_import_form_is_clean(self):
        module_dir = os.path.join(self.tmpdir, "test_mod")
        _write(os.path.join(module_dir, "__init__.py"), "from . import controllers\nfrom . import models\n")
        _write(os.path.join(module_dir, "models", "__init__.py"), "from . import thing\n")
        errors = chk._check_module("test_mod", module_dir)
        self.assertEqual(errors, [])

    def test_a_models_subpackage_imported_via_from_dot_models_import_form_is_clean(self):
        # The other real import shape this codebase uses: `from .models import Foo`
        # rather than `from . import models`.
        module_dir = os.path.join(self.tmpdir, "test_mod")
        _write(os.path.join(module_dir, "__init__.py"), "from .models import Thing\n")
        _write(os.path.join(module_dir, "models", "__init__.py"), "class Thing: pass\n")
        errors = chk._check_module("test_mod", module_dir)
        self.assertEqual(errors, [])

    def test_a_module_with_no_subpackages_at_all_is_clean(self):
        module_dir = os.path.join(self.tmpdir, "test_mod")
        _write(os.path.join(module_dir, "__init__.py"), "\n")
        errors = chk._check_module("test_mod", module_dir)
        self.assertEqual(errors, [])

    def test_controllers_and_wizard_are_checked_independently(self):
        module_dir = os.path.join(self.tmpdir, "test_mod")
        _write(os.path.join(module_dir, "__init__.py"), "from . import models\n")
        _write(os.path.join(module_dir, "models", "__init__.py"), "\n")
        _write(os.path.join(module_dir, "controllers", "__init__.py"), "\n")
        _write(os.path.join(module_dir, "wizard", "__init__.py"), "\n")
        errors = chk._check_module("test_mod", module_dir)
        self.assertEqual(len(errors), 2)
        joined = " ".join(errors)
        self.assertIn("controllers", joined)
        self.assertIn("wizard", joined)

    def test_a_module_with_no_top_level_init_at_all_is_not_this_checkers_problem(self):
        module_dir = os.path.join(self.tmpdir, "test_mod")
        _write(os.path.join(module_dir, "models", "__init__.py"), "\n")
        errors = chk._check_module("test_mod", module_dir)
        self.assertEqual(errors, [])


class ResolveRepoRootTests(unittest.TestCase):
    # Regression test for the same real bug check_access_csv_group_order.py's own
    # ResolveRepoRootTests covers: run_linters.py's dir_path resolves to .../hams_shared, not a
    # real repo root, which silently made this checker find zero modules too.
    def test_a_hams_shared_path_redirects_to_its_parent_repo(self):
        # _resolve_repo_root is pure path-string logic (no filesystem access), so a
        # synthetic, non-existent path exercises it exactly as well as a real one.
        fake_repo = os.path.join(os.sep, "some", "workspace", "some_repo")
        self.assertEqual(
            chk._resolve_repo_root(os.path.join(fake_repo, "hams_shared")),
            fake_repo,
        )

    def test_a_real_repo_root_passes_through_unchanged(self):
        fake_repo = os.path.join(os.sep, "some", "workspace", "some_repo")
        self.assertEqual(chk._resolve_repo_root(fake_repo), fake_repo)


class MainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def test_main_returns_nonzero_when_a_real_module_on_disk_has_the_bug(self):
        module_dir = os.path.join(self.tmpdir, "broken_mod")
        _write(os.path.join(module_dir, "__manifest__.py"), "{'name': 'x', 'depends': []}\n")
        _write(os.path.join(module_dir, "__init__.py"), "from . import controllers\n")
        _write(os.path.join(module_dir, "models", "__init__.py"), "\n")

        old_argv = sys.argv
        sys.argv = ["check_module_subpackage_imports.py", self.tmpdir]
        try:
            self.assertEqual(chk.main(), 1)
        finally:
            sys.argv = old_argv

    def test_main_returns_zero_for_a_clean_module(self):
        module_dir = os.path.join(self.tmpdir, "clean_mod")
        _write(os.path.join(module_dir, "__manifest__.py"), "{'name': 'x', 'depends': []}\n")
        _write(os.path.join(module_dir, "__init__.py"), "from . import models\n")
        _write(os.path.join(module_dir, "models", "__init__.py"), "\n")

        old_argv = sys.argv
        sys.argv = ["check_module_subpackage_imports.py", self.tmpdir]
        try:
            self.assertEqual(chk.main(), 0)
        finally:
            sys.argv = old_argv


if __name__ == "__main__":
    unittest.main()
