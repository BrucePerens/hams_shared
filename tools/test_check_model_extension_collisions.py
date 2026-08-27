#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_model_extension_collisions.py (ADR 0086).

This script is well-factored into pure functions (_is_model_class,
_literal_str_or_list, _extract_class_info, _scan), so these tests exercise
those directly plus _scan against real temp-directory module fixtures on
disk -- the same pattern already used for check_dependency_cycles.py.
"""

import ast
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_model_extension_collisions as chk  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _class_node(src):
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            return node
    raise AssertionError("no class found in fixture source")


class IsModelClassTests(unittest.TestCase):
    def test_recognizes_models_dot_model(self):
        node = _class_node("class Foo(models.Model):\n    pass\n")
        self.assertTrue(chk._is_model_class(node))

    def test_recognizes_a_bare_model_import_form(self):
        node = _class_node("class Foo(Model):\n    pass\n")
        self.assertTrue(chk._is_model_class(node))

    def test_recognizes_abstract_and_transient_model(self):
        self.assertTrue(chk._is_model_class(_class_node("class Foo(models.AbstractModel):\n    pass\n")))
        self.assertTrue(chk._is_model_class(_class_node("class Foo(models.TransientModel):\n    pass\n")))

    def test_a_plain_class_is_not_a_model_class(self):
        node = _class_node("class Foo:\n    pass\n")
        self.assertFalse(chk._is_model_class(node))

    def test_a_non_model_base_is_not_a_model_class(self):
        node = _class_node("class Foo(SomeMixin):\n    pass\n")
        self.assertFalse(chk._is_model_class(node))


class LiteralStrOrListTests(unittest.TestCase):
    def _value_node(self, src):
        tree = ast.parse(src)
        return tree.body[0].value

    def test_a_single_string_becomes_a_one_item_list(self):
        self.assertEqual(chk._literal_str_or_list(self._value_node('x = "ham.qso"')), ["ham.qso"])

    def test_a_list_of_strings_is_returned_as_is(self):
        self.assertEqual(
            chk._literal_str_or_list(self._value_node('x = ["ham.qso", "mail.thread"]')),
            ["ham.qso", "mail.thread"],
        )

    def test_a_non_literal_expression_returns_none(self):
        self.assertIsNone(chk._literal_str_or_list(self._value_node("x = some_function_call()")))

    def test_a_list_containing_a_non_string_returns_none(self):
        self.assertIsNone(chk._literal_str_or_list(self._value_node("x = [1, 2]")))


class ExtractClassInfoTests(unittest.TestCase):
    def test_extracts_name_only(self):
        node = _class_node('class Foo(models.Model):\n    _name = "ham.qso"\n')
        name, inherit, auto_false, has_init = chk._extract_class_info(node)
        self.assertEqual(name, ["ham.qso"])
        self.assertIsNone(inherit)
        self.assertFalse(auto_false)
        self.assertFalse(has_init)

    def test_extracts_self_referencing_name_plus_inherit(self):
        node = _class_node(
            'class Foo(models.Model):\n'
            '    _name = "res.users"\n'
            '    _inherit = ["res.users", "edge.routing.mixin"]\n'
        )
        name, inherit, _auto_false, _has_init = chk._extract_class_info(node)
        self.assertEqual(name, ["res.users"])
        self.assertEqual(inherit, ["res.users", "edge.routing.mixin"])

    def test_detects_auto_false(self):
        node = _class_node('class Foo(models.Model):\n    _name = "x"\n    _auto = False\n')
        _name, _inherit, auto_false, _has_init = chk._extract_class_info(node)
        self.assertTrue(auto_false)

    def test_detects_an_init_override(self):
        node = _class_node(
            'class Foo(models.Model):\n'
            '    _name = "x"\n'
            '    _auto = False\n'
            '    def init(self):\n'
            '        pass\n'
        )
        _name, _inherit, _auto_false, has_init = chk._extract_class_info(node)
        self.assertTrue(has_init)

    def test_a_bare_inherit_only_declaration_has_no_name(self):
        node = _class_node('class Foo(models.Model):\n    _inherit = "ham.qso"\n')
        name, inherit, _auto_false, _has_init = chk._extract_class_info(node)
        self.assertIsNone(name)
        self.assertEqual(inherit, ["ham.qso"])


class ResolveRepoRootTests(unittest.TestCase):
    # Regression test for a real bug found the same night check_access_csv_group_order.py's own
    # identical bug was found and fixed: run_linters.py's own dir_path resolves to
    # .../hams_shared, not a real repo root -- confirmed directly, this checker (run_linters.py's
    # own step 22, the ADR 0086 hard gate) was silently scanning 0 of the real 182 models via
    # run_linters.py's actual invocation, and reporting a false clean pass, since this checker is
    # silent on success by design (see main()) and never printed anything to reveal it was
    # checking nothing.
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
    # of this checker's dual-repo scan had a regression test, even though the sibling-finding
    # half is exactly the piece that determines whether both repos' models actually get merged.
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


class ScanIntegrationTests(unittest.TestCase):
    """Exercises _scan() against real temp-directory module fixtures, the
    same way its actual filesystem-walking callers use it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _module(self, name, py_content, filename="models.py"):
        _write(os.path.join(self.tmp, name, "__manifest__.py"), "{}\n")
        _write(os.path.join(self.tmp, name, filename), py_content)

    def test_a_single_owner_model_is_not_flagged(self):
        self._module("mod_a", 'class Foo(models.Model):\n    _name = "ham.qso"\n')
        _name_owners, claiming_owners, _auto_false, _inherit_only = chk._scan([self.tmp])
        self.assertEqual(len(claiming_owners.get("ham.qso", [])), 1)

    def test_two_modules_both_declaring_name_is_an_ambiguous_multi_owner(self):
        self._module("mod_a", 'class Foo(models.Model):\n    _name = "ham.dx.spot"\n')
        self._module("mod_b", 'class Bar(models.Model):\n    _name = "ham.dx.spot"\n')
        _name_owners, claiming_owners, _auto_false, _inherit_only = chk._scan([self.tmp])
        modules = {m for m, _f, _l in claiming_owners["ham.dx.spot"]}
        self.assertEqual(modules, {"mod_a", "mod_b"})

    def test_a_self_referencing_name_plus_inherit_is_excluded_from_claiming_owners(self):
        # The documented "extend + add a mixin" idiom: _name = X and
        # _inherit containing X is not a second ownership claim.
        self._module("mod_a", 'class Foo(models.Model):\n    _name = "res.users"\n')
        self._module(
            "mod_b",
            'class Bar(models.Model):\n'
            '    _name = "res.users"\n'
            '    _inherit = ["res.users", "edge.routing.mixin"]\n',
        )
        _name_owners, claiming_owners, _auto_false, _inherit_only = chk._scan([self.tmp])
        modules = {m for m, _f, _l in claiming_owners["res.users"]}
        self.assertEqual(modules, {"mod_a"})

    def test_an_auto_false_model_extended_by_another_module_is_detected_via_main(self):
        self._module(
            "mod_owner",
            'class Foo(models.Model):\n'
            '    _name = "ham.repeater.public.view"\n'
            '    _auto = False\n'
            '    def init(self):\n'
            '        pass\n',
        )
        self._module(
            "mod_extender",
            'class Bar(models.Model):\n    _inherit = "ham.repeater.public.view"\n',
        )
        import subprocess
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_model_extension_collisions.py")
        result = subprocess.run([sys.executable, script, self.tmp], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("CROSS-MODULE EXTENSION OF AN _auto=False MODEL", result.stdout)

    def test_an_inherit_only_class_of_a_normal_auto_true_model_is_fine(self):
        self._module("mod_owner", 'class Foo(models.Model):\n    _name = "ham.qso"\n')
        self._module("mod_extender", 'class Bar(models.Model):\n    _inherit = "ham.qso"\n')
        import subprocess
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_model_extension_collisions.py")
        result = subprocess.run([sys.executable, script, self.tmp], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_a_syntax_broken_file_is_skipped_without_crashing(self):
        _write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        _write(os.path.join(self.tmp, "mod_a", "models.py"), "class Foo(models.Model: broken syntax")
        name_owners, _claiming_owners, _auto_false, _inherit_only = chk._scan([self.tmp])
        self.assertEqual(name_owners, {})

    def test_a_file_not_under_any_manifest_is_skipped(self):
        _write(os.path.join(self.tmp, "orphan.py"), 'class Foo(models.Model):\n    _name = "ham.qso"\n')
        name_owners, _claiming_owners, _auto_false, _inherit_only = chk._scan([self.tmp])
        self.assertEqual(name_owners, {})

    def test_a_malformed_auto_value_does_not_crash_the_scan(self):
        # The _auto = ... literal_eval() has its own ValueError/SyntaxError guard --
        # a non-literal RHS (a name reference, not a constant) must not crash the scan.
        self._module(
            "mod_a",
            'class Foo(models.Model):\n'
            '    _name = "ham.qso"\n'
            '    _auto = some_computed_flag\n',
        )
        name_owners, _claiming_owners, auto_false, _inherit_only = chk._scan([self.tmp])
        self.assertIn("ham.qso", name_owners)
        self.assertNotIn("ham.qso", auto_false)


class ModuleOfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_the_nearest_ancestor_manifest_directory(self):
        _write(os.path.join(self.tmp, "ham_qso", "__manifest__.py"), "{}\n")
        fpath = os.path.join(self.tmp, "ham_qso", "models", "res_users.py")
        _write(fpath, "")
        cache = {}
        self.assertEqual(chk._module_of(fpath, cache), "ham_qso")

    def test_a_file_with_no_manifest_anywhere_up_the_tree_returns_none(self):
        fpath = os.path.join(self.tmp, "scripts", "helper.py")
        _write(fpath, "")
        cache = {}
        self.assertIsNone(chk._module_of(fpath, cache))

    def test_the_cache_is_populated_and_reused_for_a_second_lookup(self):
        _write(os.path.join(self.tmp, "ham_qso", "__manifest__.py"), "{}\n")
        first = os.path.join(self.tmp, "ham_qso", "models", "res_users.py")
        second = os.path.join(self.tmp, "ham_qso", "models", "res_partner.py")
        _write(first, "")
        _write(second, "")
        cache = {}
        result1 = chk._module_of(first, cache)
        # The intermediate "models" directory must now be a cache key, populated by the
        # walked-list backfill loop after the first lookup found the real manifest.
        self.assertIn(os.path.join(self.tmp, "ham_qso", "models"), cache)
        result2 = chk._module_of(second, cache)
        self.assertEqual(result1, result2)
        self.assertEqual(result2, "ham_qso")

    def test_a_cache_hit_short_circuits_without_touching_the_filesystem_again(self):
        _write(os.path.join(self.tmp, "ham_qso", "__manifest__.py"), "{}\n")
        models_dir = os.path.join(self.tmp, "ham_qso", "models")
        cache = {models_dir: "ham_qso_cached_value"}
        fpath = os.path.join(models_dir, "res_users.py")
        _write(fpath, "")
        self.assertEqual(chk._module_of(fpath, cache), "ham_qso_cached_value")


if __name__ == "__main__":
    unittest.main()
