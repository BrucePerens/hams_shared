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

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_access_csv_group_order as chk  # noqa: E402

_SETTINGS = settings(max_examples=200, deadline=None)


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


def _write_group_xml(path, group_ids):
    """Writes one res.groups <record> per id in group_ids (possibly zero)."""
    records = "\n".join(
        f'        <record id="{g}" model="res.groups">\n'
        f'            <field name="name">{g}</field>\n'
        f"        </record>"
        for g in group_ids
    )
    _write(
        path,
        f'<?xml version="1.0" encoding="utf-8"?>\n<odoo>\n    <data>\n{records}\n    </data>\n</odoo>\n',
    )


def _write_access_csv(path, module_name, referenced_groups, include_cross_module_ref):
    rows = ["id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink"]
    for i, g in enumerate(referenced_groups):
        rows.append(f"access_{i},thing_{i},model_thing,{module_name}.{g},1,1,1,1")
    if include_cross_module_ref:
        rows.append("access_cross,thing_cross,model_thing,base.group_system,1,1,1,1")
    _write(path, "\n".join(rows) + "\n")


_SAFE_NAME = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=3, max_size=8)


@st.composite
def _module_layout(draw):
    # docs/proposals/CODE_REVIEW_PROCESS.md's standing Hypothesis invitation, applied to a
    # second checker in the same "small, pure, structured-input" family as
    # check_xml_comment_double_hyphen.py -- this one exercises the full pipeline (AST manifest
    # parsing, regex XML group extraction, CSV parsing, and the before/anywhere ordering logic
    # together), not just one offset computation. Builds a random module: a pool of groups, zero
    # or more XML files each defining a random subset of them, one access CSV referencing a
    # random subset of same-module groups (optionally plus one cross-module reference), and a
    # random interleaving of the XML files and the CSV in the manifest's own 'data' list -- then
    # independently (outside the checker) works out which referenced groups are actually defined
    # in a data-list position before the CSV, so the property test's expectation is computed from
    # the generated layout directly, not by re-deriving the checker's own algorithm.
    module_name = draw(_SAFE_NAME)
    num_groups = draw(st.integers(min_value=1, max_value=4))
    group_ids = [f"group_{i}" for i in range(num_groups)]
    num_xml = draw(st.integers(min_value=0, max_value=3))
    xml_names = [f"data_{i}.xml" for i in range(num_xml)]

    group_to_xml = {}
    for g in group_ids:
        if num_xml:
            choice = draw(st.integers(min_value=-1, max_value=num_xml - 1))
            if choice >= 0:
                group_to_xml[g] = xml_names[choice]

    order = draw(st.permutations(xml_names))
    csv_pos = draw(st.integers(min_value=0, max_value=len(order)))
    data_list = list(order[:csv_pos]) + ["security/ir.model.access.csv"] + list(order[csv_pos:])

    referenced = draw(st.lists(st.sampled_from(group_ids), unique=True, max_size=num_groups))
    include_cross_module_ref = draw(st.booleans())

    return module_name, group_ids, group_to_xml, xml_names, data_list, referenced, include_cross_module_ref


class CheckModulePropertyTests(unittest.TestCase):
    @given(_module_layout())
    @_SETTINGS
    def test_flags_exactly_the_groups_not_defined_before_the_csv(self, generated):
        (
            module_name,
            group_ids,
            group_to_xml,
            xml_names,
            data_list,
            referenced,
            include_cross_module_ref,
        ) = generated

        tmpdir = tempfile.mkdtemp()
        try:
            module_dir = os.path.join(tmpdir, module_name)
            _write(
                os.path.join(module_dir, "__manifest__.py"),
                _MANIFEST_TEMPLATE.format(
                    data_entries=", ".join(f'"{d}"' for d in data_list)
                ),
            )
            for xml_name in xml_names:
                groups_here = [g for g, x in group_to_xml.items() if x == xml_name]
                _write_group_xml(os.path.join(module_dir, xml_name), groups_here)
            _write_access_csv(
                os.path.join(module_dir, "security", "ir.model.access.csv"),
                module_name,
                referenced,
                include_cross_module_ref,
            )

            errors = chk._check_module(
                module_name, module_dir, os.path.join(module_dir, "__manifest__.py")
            )

            csv_idx = data_list.index("security/ir.model.access.csv")
            defined_before = {
                g for g, xml_name in group_to_xml.items() if xml_name in data_list[:csv_idx]
            }
            defined_anywhere = set(group_to_xml.keys())

            expected_later = {
                g for g in referenced if g not in defined_before and g in defined_anywhere
            }
            expected_missing = {g for g in referenced if g not in defined_anywhere}

            self.assertEqual(
                len(errors),
                len(expected_later) + len(expected_missing),
                f"[!] DIAGNOSTIC FOR AI: module={module_name} data_list={data_list} "
                f"group_to_xml={group_to_xml} referenced={referenced} errors={errors!r}",
            )
            for g in expected_later:
                self.assertTrue(
                    any(f"{module_name}.{g}" in e and "listed LATER" in e for e in errors),
                    f"expected a 'listed LATER' error for {module_name}.{g}, got {errors!r}",
                )
            for g in expected_missing:
                self.assertTrue(
                    any(
                        f"{module_name}.{g}" in e and "not defined by any XML file" in e
                        for e in errors
                    ),
                    f"expected a 'not defined' error for {module_name}.{g}, got {errors!r}",
                )
            if include_cross_module_ref:
                self.assertTrue(all("base.group_system" not in e for e in errors))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
