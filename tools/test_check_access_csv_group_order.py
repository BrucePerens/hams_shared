#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_access_csv_group_order.py.

Regression test for the real bug that motivated this checker: ham_aprs/__manifest__.py listed
security/ir.model.access.csv before security/security_data.xml, which defines the
ham_aprs.group_aprs_service group the CSV references -- Odoo's whole boot failed at that module,
taking down the entire 142-module test suite with it, not just ham_aprs's own tests.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_access_csv_group_order as chk  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


_ACCESS_CSV = (
    "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
    "access_thing,thing,model_thing,{group_ref},1,1,1,1\n"
)

_GROUP_XML = """<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <data>
        <record id="{group_id}" model="res.groups">
            <field name="name">Service Group</field>
        </record>
    </data>
</odoo>
"""

_MANIFEST_TEMPLATE = """{{
    "name": "Test Module",
    "depends": ["base"],
    "data": [{data_entries}],
}}
"""


class CheckModuleTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def _make_module(self, module_name, data_list, csv_group_ref, group_id="group_service"):
        module_dir = os.path.join(self.tmpdir, module_name)
        data_entries = ", ".join(f'"{d}"' for d in data_list)
        _write(
            os.path.join(module_dir, "__manifest__.py"),
            _MANIFEST_TEMPLATE.format(data_entries=data_entries),
        )
        if "security/ir.model.access.csv" in data_list:
            _write(
                os.path.join(module_dir, "security", "ir.model.access.csv"),
                _ACCESS_CSV.format(group_ref=csv_group_ref),
            )
        if "security/security_data.xml" in data_list:
            _write(
                os.path.join(module_dir, "security", "security_data.xml"),
                _GROUP_XML.format(group_id=group_id),
            )
        return module_name, module_dir, os.path.join(module_dir, "__manifest__.py")

    def test_group_defined_after_the_csv_that_needs_it_is_flagged(self):
        # Regression test for the real ham_aprs bug this checker exists to catch.
        name, module_dir, manifest_path = self._make_module(
            "test_mod",
            ["security/ir.model.access.csv", "security/security_data.xml"],
            "test_mod.group_service",
        )
        errors = chk._check_module(name, module_dir, manifest_path)
        self.assertEqual(len(errors), 1)
        self.assertIn("test_mod.group_service", errors[0])
        self.assertIn("defined in this module but in an XML file listed LATER", errors[0])

    def test_group_defined_before_the_csv_that_needs_it_is_clean(self):
        # The actual fix applied to ham_aprs's own manifest: swap the order.
        name, module_dir, manifest_path = self._make_module(
            "test_mod",
            ["security/security_data.xml", "security/ir.model.access.csv"],
            "test_mod.group_service",
        )
        errors = chk._check_module(name, module_dir, manifest_path)
        self.assertEqual(errors, [])

    def test_a_reference_to_another_modules_group_is_never_flagged(self):
        # base.group_system and similar are always safe regardless of this
        # module's own data order -- dependencies fully install first.
        name, module_dir, manifest_path = self._make_module(
            "test_mod",
            ["security/ir.model.access.csv"],
            "base.group_system",
        )
        errors = chk._check_module(name, module_dir, manifest_path)
        self.assertEqual(errors, [])

    def test_a_group_never_defined_anywhere_gets_the_typo_flavored_message(self):
        name, module_dir, manifest_path = self._make_module(
            "test_mod",
            ["security/ir.model.access.csv"],
            "test_mod.group_that_does_not_exist",
        )
        errors = chk._check_module(name, module_dir, manifest_path)
        self.assertEqual(len(errors), 1)
        self.assertIn("not defined by any XML file", errors[0])

    def test_a_module_with_no_csv_at_all_is_clean(self):
        module_dir = os.path.join(self.tmpdir, "no_csv_mod")
        _write(
            os.path.join(module_dir, "__manifest__.py"),
            _MANIFEST_TEMPLATE.format(data_entries='"views/thing_views.xml"'),
        )
        errors = chk._check_module("no_csv_mod", module_dir, os.path.join(module_dir, "__manifest__.py"))
        self.assertEqual(errors, [])


class ResolveRepoRootTests(unittest.TestCase):
    # Regression test for a real bug found the same night this checker was built:
    # run_linters.py's own dir_path resolves to .../hams_shared (its __file__ lives inside
    # hams_shared/tools/), not a real repo root -- this checker was silently finding zero
    # modules and printing a false "OK" when actually invoked from run_linters.py, despite
    # every direct/manual invocation used to build and verify it working correctly.
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


class FindSiblingRepoTests(unittest.TestCase):
    # _find_sibling_repo() itself was never independently tested -- only the resolve-root half
    # of this checker's dual-repo scan had a regression test.
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.workspace = os.path.join(self.tmp, "workspace")
        os.makedirs(self.workspace)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_repo(self, name, module_names=()):
        repo = os.path.join(self.workspace, name)
        for module_name in module_names:
            path = os.path.join(repo, module_name, "__manifest__.py")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}")
        return repo

    def test_hams_open_finds_a_real_hams_com_sibling(self):
        hams_open = self._make_repo("hams_open", module_names=["zero_sudo"])
        hams_com = self._make_repo("hams_com", module_names=["ham_base"])
        self.assertEqual(chk._find_sibling_repo(hams_open), hams_com)

    def test_no_sibling_directory_present_returns_none(self):
        repo = self._make_repo("hams_open", module_names=["zero_sudo"])
        self.assertIsNone(chk._find_sibling_repo(repo))

    def test_a_sibling_directory_with_no_real_module_in_it_returns_none(self):
        repo = self._make_repo("hams_open", module_names=["zero_sudo"])
        os.makedirs(os.path.join(self.workspace, "hams_com", "not_a_module"))
        self.assertIsNone(chk._find_sibling_repo(repo))


class MainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def test_main_returns_nonzero_when_a_real_module_on_disk_has_the_bug(self):
        module_dir = os.path.join(self.tmpdir, "broken_mod")
        _write(
            os.path.join(module_dir, "__manifest__.py"),
            _MANIFEST_TEMPLATE.format(
                data_entries='"security/ir.model.access.csv", "security/security_data.xml"'
            ),
        )
        _write(
            os.path.join(module_dir, "security", "ir.model.access.csv"),
            _ACCESS_CSV.format(group_ref="broken_mod.group_service"),
        )
        _write(
            os.path.join(module_dir, "security", "security_data.xml"),
            _GROUP_XML.format(group_id="group_service"),
        )

        old_argv = sys.argv
        sys.argv = ["check_access_csv_group_order.py", self.tmpdir]
        try:
            self.assertEqual(chk.main(), 1)
        finally:
            sys.argv = old_argv

    def test_main_returns_zero_for_a_clean_module(self):
        module_dir = os.path.join(self.tmpdir, "clean_mod")
        _write(
            os.path.join(module_dir, "__manifest__.py"),
            _MANIFEST_TEMPLATE.format(
                data_entries='"security/security_data.xml", "security/ir.model.access.csv"'
            ),
        )
        _write(
            os.path.join(module_dir, "security", "ir.model.access.csv"),
            _ACCESS_CSV.format(group_ref="clean_mod.group_service"),
        )
        _write(
            os.path.join(module_dir, "security", "security_data.xml"),
            _GROUP_XML.format(group_id="group_service"),
        )

        old_argv = sys.argv
        sys.argv = ["check_access_csv_group_order.py", self.tmpdir]
        try:
            self.assertEqual(chk.main(), 0)
        finally:
            sys.argv = old_argv


if __name__ == "__main__":
    unittest.main()
