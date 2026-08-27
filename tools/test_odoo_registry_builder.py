#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for odoo_registry_builder.py (ODOO_AWARE_TYPE_CHECKING.md Phase 2 foundation).
"""

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import odoo_registry_builder as orb  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "odoo_registry_builder.py")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _class_node(src):
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            return node
    raise AssertionError("no class in fixture")


def _call_node(src):
    tree = ast.parse(src)
    return tree.body[0].value


class IsModelClassTests(unittest.TestCase):
    def test_recognizes_models_dot_model(self):
        self.assertTrue(orb._is_model_class(_class_node("class Foo(models.Model):\n    pass\n")))

    def test_a_plain_class_is_not_a_model_class(self):
        self.assertFalse(orb._is_model_class(_class_node("class Foo:\n    pass\n")))


class LiteralStrOrListTests(unittest.TestCase):
    def test_a_single_string_becomes_a_one_item_list(self):
        node = ast.parse('x = "ham.qso"').body[0].value
        self.assertEqual(orb._literal_str_or_list(node), ["ham.qso"])

    def test_a_non_literal_returns_none(self):
        node = ast.parse("x = some_call()").body[0].value
        self.assertIsNone(orb._literal_str_or_list(node))


class FieldCallInfoTests(unittest.TestCase):
    def test_a_plain_char_field_has_no_comodel(self):
        node = _call_node("fields.Char(string='Name')")
        self.assertEqual(orb._field_call_info(node), ("Char", None))

    def test_a_many2one_with_a_literal_comodel_string(self):
        node = _call_node("fields.Many2one('res.partner')")
        self.assertEqual(orb._field_call_info(node), ("Many2one", "res.partner"))

    def test_a_bare_many2one_import_form_is_also_recognized(self):
        node = _call_node("Many2one('res.partner')")
        self.assertEqual(orb._field_call_info(node), ("Many2one", "res.partner"))

    def test_a_many2one_with_no_positional_args_has_no_comodel(self):
        node = _call_node("fields.Many2one(string='Owner')")
        self.assertEqual(orb._field_call_info(node), ("Many2one", None))

    def test_a_many2one_whose_first_arg_is_a_variable_has_no_comodel(self):
        # Only a literal string constant is resolved -- a dynamically
        # referenced comodel name can't be, and isn't guessed at.
        node = _call_node("fields.Many2one(SOME_MODEL_CONST)")
        self.assertEqual(orb._field_call_info(node), ("Many2one", None))

    def test_an_unrecognized_call_returns_none(self):
        node = _call_node("some_helper_function()")
        self.assertIsNone(orb._field_call_info(node))


class ExtractFieldsTests(unittest.TestCase):
    def test_extracts_multiple_field_assignments(self):
        node = _class_node(
            "class Foo(models.Model):\n"
            "    name = fields.Char()\n"
            "    partner_id = fields.Many2one('res.partner')\n"
        )
        fields_found = orb._extract_fields(node, "mod_a", "foo.py")
        names = {f.name for f in fields_found}
        self.assertEqual(names, {"name", "partner_id"})

    def test_a_non_field_assignment_is_ignored(self):
        node = _class_node("class Foo(models.Model):\n    _name = 'x'\n")
        self.assertEqual(orb._extract_fields(node, "mod_a", "foo.py"), [])

    def test_a_multi_target_assignment_yields_a_field_for_each_target(self):
        node = _class_node("class Foo(models.Model):\n    a = b = fields.Char()\n")
        fields_found = orb._extract_fields(node, "mod_a", "foo.py")
        names = {f.name for f in fields_found}
        self.assertEqual(names, {"a", "b"})


class ArgsInfoTests(unittest.TestCase):
    def _args(self, src):
        tree = ast.parse(src)
        return tree.body[0].args

    def test_a_simple_method_with_defaults(self):
        args_node = self._args("def f(self, a, b=1, *args, c, **kwargs): pass")
        arg_names, posonly, n_defaults, has_varargs, has_varkw, kwonly = orb._args_info(args_node)
        self.assertEqual(arg_names, ["self", "a", "b"])
        self.assertEqual(posonly, 0)
        self.assertEqual(n_defaults, 1)
        self.assertTrue(has_varargs)
        self.assertTrue(has_varkw)
        self.assertEqual(kwonly, ["c"])

    def test_positional_only_args_are_included_in_arg_names(self):
        args_node = self._args("def f(self, a, /, b): pass")
        arg_names, posonly, *_rest = orb._args_info(args_node)
        self.assertEqual(arg_names, ["self", "a", "b"])
        self.assertEqual(posonly, 2)


class ExtractMethodsTests(unittest.TestCase):
    def test_extracts_a_method_with_the_real_min_args_semantics(self):
        # posonly_count on MethodInfo is computed here as
        # len(arg_names) - n_defaults -- NOT Python's own "positional-only
        # parameter count" concept despite the name -- it's this builder's
        # own "minimum required positional args" figure. Locking that in:
        # def f(self, a, b=1) -> arg_names=[self,a,b], n_defaults=1, so
        # posonly_count must be 2 (self, a -- both required), not 0.
        node = _class_node(
            "class Foo(models.Model):\n    def f(self, a, b=1):\n        pass\n"
        )
        methods = orb._extract_methods(node, "mod_a", "foo.py")
        self.assertEqual(len(methods), 1)
        m = methods[0]
        self.assertEqual(m.name, "f")
        self.assertEqual(m.arg_names, ["self", "a", "b"])
        self.assertEqual(m.posonly_count, 2)
        self.assertEqual(m.min_args(), 2)

    def test_max_args_is_none_when_the_method_accepts_varargs(self):
        node = _class_node(
            "class Foo(models.Model):\n    def f(self, *args):\n        pass\n"
        )
        m = orb._extract_methods(node, "mod_a", "foo.py")[0]
        self.assertIsNone(m.max_args())

    def test_max_args_is_the_full_arg_count_without_varargs(self):
        node = _class_node(
            "class Foo(models.Model):\n    def f(self, a, b):\n        pass\n"
        )
        m = orb._extract_methods(node, "mod_a", "foo.py")[0]
        self.assertEqual(m.max_args(), 3)


class ExtractClassInfoTests(unittest.TestCase):
    def test_gathers_name_inherit_fields_and_methods_together(self):
        node = _class_node(
            "class Foo(models.Model):\n"
            "    _name = 'ham.qso'\n"
            "    _inherit = ['ham.qso', 'mail.thread']\n"
            "    callsign = fields.Char()\n"
            "    def action_confirm(self):\n"
            "        pass\n"
        )
        name_values, inherit_values, fields_found, methods_found = orb._extract_class_info(
            node, "mod_a", "foo.py"
        )
        self.assertEqual(name_values, ["ham.qso"])
        self.assertEqual(inherit_values, ["ham.qso", "mail.thread"])
        self.assertEqual(len(fields_found), 1)
        self.assertEqual(len(methods_found), 1)


class MergeIntoTests(unittest.TestCase):
    def test_creates_a_new_merged_model_entry(self):
        registry = {}
        orb._merge_into(registry, "ham.qso", "mod_a", "foo.py", 3, "Foo", [], [])
        self.assertIn("ham.qso", registry)
        self.assertEqual(len(registry["ham.qso"].contributors), 1)

    def test_a_second_contributor_appends_rather_than_replaces(self):
        registry = {}
        orb._merge_into(registry, "ham.qso", "mod_a", "foo.py", 3, "Foo", [], [])
        orb._merge_into(registry, "ham.qso", "mod_b", "bar.py", 5, "Bar", [], [])
        self.assertEqual(len(registry["ham.qso"].contributors), 2)

    def test_a_later_contributor_redeclaring_a_field_wins(self):
        registry = {}
        node1 = _class_node("class Foo(models.Model):\n    name = fields.Char()\n")
        node2 = _class_node("class Bar(models.Model):\n    name = fields.Text()\n")
        f1 = orb._extract_fields(node1, "mod_a", "foo.py")
        f2 = orb._extract_fields(node2, "mod_b", "bar.py")
        orb._merge_into(registry, "ham.qso", "mod_a", "foo.py", 1, "Foo", f1, [])
        orb._merge_into(registry, "ham.qso", "mod_b", "bar.py", 2, "Bar", f2, [])
        self.assertEqual(registry["ham.qso"].fields["name"].field_type, "Text")

    def test_the_earlier_contributor_is_not_silently_lost_from_history(self):
        # Regression test for the user's own direct objection to the
        # previous behavior: "resolved" last-wins is fine as one answer,
        # but the earlier contributor must still be inspectable, not
        # discarded -- that silent loss is exactly what made an
        # accidental cross-module name collision indistinguishable from a
        # deliberate Odoo _inherit override.
        registry = {}
        node1 = _class_node("class Foo(models.Model):\n    name = fields.Char()\n")
        node2 = _class_node("class Bar(models.Model):\n    name = fields.Text()\n")
        f1 = orb._extract_fields(node1, "mod_a", "foo.py")
        f2 = orb._extract_fields(node2, "mod_b", "bar.py")
        orb._merge_into(registry, "ham.qso", "mod_a", "foo.py", 1, "Foo", f1, [])
        orb._merge_into(registry, "ham.qso", "mod_b", "bar.py", 2, "Bar", f2, [])
        history = registry["ham.qso"].field_contributions["name"]
        self.assertEqual(len(history), 2)
        self.assertEqual({c.module for c in history}, {"mod_a", "mod_b"})
        self.assertEqual({c.field_type for c in history}, {"Char", "Text"})

    def test_find_suspicious_redeclarations_flags_a_real_cross_module_collision(self):
        registry = {}
        node1 = _class_node("class Foo(models.Model):\n    def bar(self):\n        return 1\n")
        node2 = _class_node("class Baz(models.Model):\n    def bar(self):\n        return 2\n")
        m1 = orb._extract_methods(node1, "mod_a", "foo.py")
        m2 = orb._extract_methods(node2, "mod_b", "baz.py")
        orb._merge_into(registry, "ham.qso", "mod_a", "foo.py", 1, "Foo", [], m1)
        orb._merge_into(registry, "ham.qso", "mod_b", "baz.py", 2, "Baz", [], m2)
        suspicious = orb.find_suspicious_redeclarations(registry)
        self.assertIn("ham.qso", suspicious)
        self.assertIn("bar", suspicious["ham.qso"])
        contributions, likely_cooperative = suspicious["ham.qso"]["bar"]
        self.assertEqual(len(contributions), 2)
        self.assertFalse(
            likely_cooperative,
            "neither contributor calls super() -- this must NOT be classified as likely-cooperative",
        )

    def test_find_suspicious_redeclarations_treats_a_super_chain_as_likely_cooperative(self):
        registry = {}
        node1 = _class_node("class Foo(models.Model):\n    def bar(self):\n        return 1\n")
        node2 = _class_node(
            "class Baz(models.Model):\n    def bar(self):\n        return super().bar() + 1\n"
        )
        m1 = orb._extract_methods(node1, "mod_a", "foo.py")
        m2 = orb._extract_methods(node2, "mod_b", "baz.py")
        orb._merge_into(registry, "ham.qso", "mod_a", "foo.py", 1, "Foo", [], m1)
        orb._merge_into(registry, "ham.qso", "mod_b", "baz.py", 2, "Baz", [], m2)
        suspicious = orb.find_suspicious_redeclarations(registry)
        _contributions, likely_cooperative = suspicious["ham.qso"]["bar"]
        self.assertTrue(
            likely_cooperative,
            "the root definer legitimately never calls super(); only the extender needs to",
        )


class BuildRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_single_name_declaring_class_registers_its_fields(self):
        _write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        _write(
            os.path.join(self.tmp, "mod_a", "models", "ham_qso.py"),
            "class HamQSO(models.Model):\n"
            "    _name = 'ham.qso'\n"
            "    callsign = fields.Char()\n",
        )
        registry = orb.build_registry([self.tmp])
        self.assertIn("ham.qso", registry)
        self.assertIn("callsign", registry["ham.qso"].fields)

    def test_a_bare_inherit_only_class_merges_into_the_existing_model(self):
        _write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        _write(
            os.path.join(self.tmp, "mod_a", "models", "ham_qso.py"),
            "class HamQSO(models.Model):\n    _name = 'ham.qso'\n    callsign = fields.Char()\n",
        )
        _write(os.path.join(self.tmp, "mod_b", "__manifest__.py"), "{}\n")
        _write(
            os.path.join(self.tmp, "mod_b", "models", "ham_qso_ext.py"),
            "class HamQSOExt(models.Model):\n    _inherit = 'ham.qso'\n    grid_square = fields.Char()\n",
        )
        registry = orb.build_registry([self.tmp])
        self.assertEqual(set(registry["ham.qso"].fields.keys()), {"callsign", "grid_square"})
        self.assertEqual(len(registry["ham.qso"].contributors), 2)

    def test_the_self_referencing_mixin_idiom_merges_into_the_named_model(self):
        _write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        _write(
            os.path.join(self.tmp, "mod_a", "models", "res_users.py"),
            "class ResUsers(models.Model):\n"
            "    _name = 'res.users'\n"
            "    _inherit = ['res.users', 'edge.routing.mixin']\n"
            "    callsign = fields.Char()\n",
        )
        registry = orb.build_registry([self.tmp])
        self.assertIn("res.users", registry)
        self.assertIn("callsign", registry["res.users"].fields)

    def test_a_non_model_class_contributes_nothing(self):
        _write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        _write(
            os.path.join(self.tmp, "mod_a", "models", "helper.py"),
            "class Helper:\n    x = fields.Char()\n",
        )
        self.assertEqual(orb.build_registry([self.tmp]), {})

    def test_the_daemons_directory_is_never_walked(self):
        _write(os.path.join(self.tmp, "daemons", "d", "__manifest__.py"), "{}\n")
        _write(
            os.path.join(self.tmp, "daemons", "d", "models", "foo.py"),
            "class Foo(models.Model):\n    _name = 'x'\n",
        )
        self.assertEqual(orb.build_registry([self.tmp]), {})

    def test_a_syntax_broken_file_is_skipped_without_crashing(self):
        _write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        _write(os.path.join(self.tmp, "mod_a", "models", "broken.py"), "class Foo(: broken")
        self.assertEqual(orb.build_registry([self.tmp]), {})


class FindEnvGetitemTargetsTests(unittest.TestCase):
    """odoo_registry_builder.find_env_getitem_targets (ODOO_AWARE_TYPE_CHECKING.md Phase 2
    step 4's own foundation, added alongside the mypy plugin's get_method_hook)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_self_env_getitem_with_a_literal_is_found(self):
        fpath = os.path.join(self.tmp, "mod_a", "models", "foo.py")
        _write(fpath, "class Foo(models.Model):\n    def bar(self):\n        return self.env['res.users']\n")
        targets = orb.find_env_getitem_targets([self.tmp])
        self.assertEqual(targets.get(fpath), ["res.users"])

    def test_a_non_self_expression_env_getitem_is_also_found(self):
        # Odoo's real attribute is always named `env` regardless of what it hangs off --
        # confirmed as the deliberate scope of this AST match, not just a `self.env` special case.
        fpath = os.path.join(self.tmp, "mod_a", "models", "foo.py")
        _write(fpath, "class Foo(models.Model):\n    def bar(self, record):\n        return record.env['res.partner']\n")
        targets = orb.find_env_getitem_targets([self.tmp])
        self.assertEqual(targets.get(fpath), ["res.partner"])

    def test_multiple_distinct_literals_in_one_file_are_all_found_and_deduped(self):
        fpath = os.path.join(self.tmp, "mod_a", "models", "foo.py")
        _write(
            fpath,
            "class Foo(models.Model):\n"
            "    def bar(self):\n"
            "        a = self.env['res.users']\n"
            "        b = self.env['res.partner']\n"
            "        c = self.env['res.users']\n",
        )
        targets = orb.find_env_getitem_targets([self.tmp])
        self.assertEqual(targets.get(fpath), ["res.partner", "res.users"])

    def test_a_non_string_subscript_on_env_is_ignored(self):
        # A dynamically computed model name can't be resolved statically -- confirmed not
        # guessed at, same limitation _field_call_info already documents for comodel strings.
        fpath = os.path.join(self.tmp, "mod_a", "models", "foo.py")
        _write(fpath, "class Foo(models.Model):\n    def bar(self, name):\n        return self.env[name]\n")
        targets = orb.find_env_getitem_targets([self.tmp])
        self.assertNotIn(fpath, targets)

    def test_a_subscript_on_something_not_named_env_is_ignored(self):
        fpath = os.path.join(self.tmp, "mod_a", "models", "foo.py")
        _write(fpath, "class Foo(models.Model):\n    def bar(self):\n        return self.other['res.users']\n")
        targets = orb.find_env_getitem_targets([self.tmp])
        self.assertNotIn(fpath, targets)

    def test_a_file_with_no_env_getitem_at_all_is_omitted(self):
        fpath = os.path.join(self.tmp, "mod_a", "models", "foo.py")
        _write(fpath, "class Foo(models.Model):\n    def bar(self):\n        return 1\n")
        targets = orb.find_env_getitem_targets([self.tmp])
        self.assertEqual(targets, {})


class MainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *extra_args):
        result = subprocess.run(
            [sys.executable, _SCRIPT, self.tmp, *extra_args],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode, result.stdout + result.stderr

    def test_no_model_argument_prints_a_registry_summary(self):
        _write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        _write(
            os.path.join(self.tmp, "mod_a", "models", "ham_qso.py"),
            "class HamQSO(models.Model):\n    _name = 'ham.qso'\n    callsign = fields.Char()\n",
        )
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertIn("Registry built: 1 models", out)

    def test_a_named_model_argument_prints_its_full_detail(self):
        _write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        _write(
            os.path.join(self.tmp, "mod_a", "models", "ham_qso.py"),
            "class HamQSO(models.Model):\n    _name = 'ham.qso'\n    callsign = fields.Char()\n",
        )
        code, out = self._run("ham.qso")
        self.assertEqual(code, 0, out)
        self.assertIn("=== ham.qso ===", out)
        self.assertIn("callsign", out)

    def test_an_unknown_model_name_is_reported_as_not_found(self):
        _write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        code, out = self._run("does.not.exist")
        self.assertEqual(code, 0, out)
        self.assertIn("not found in registry", out)


class ManifestDependsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_reads_the_depends_list_out_of_a_real_manifest(self):
        path = os.path.join(self.tmp, "__manifest__.py")
        _write(path, "{'name': 'Thing', 'depends': ['base', 'website']}\n")
        self.assertEqual(orb._manifest_depends(path), ['base', 'website'])

    def test_a_missing_manifest_file_returns_an_empty_list(self):
        path = os.path.join(self.tmp, "does_not_exist", "__manifest__.py")
        self.assertEqual(orb._manifest_depends(path), [])

    def test_a_syntax_broken_manifest_returns_an_empty_list(self):
        path = os.path.join(self.tmp, "__manifest__.py")
        _write(path, "{'name': 'Thing', 'depends': [\n")
        self.assertEqual(orb._manifest_depends(path), [])

    def test_a_manifest_with_no_depends_key_returns_an_empty_list(self):
        path = os.path.join(self.tmp, "__manifest__.py")
        _write(path, "{'name': 'Thing'}\n")
        self.assertEqual(orb._manifest_depends(path), [])


class FindOdooCoreAddonsPathTests(unittest.TestCase):
    def test_returns_a_real_existing_directory_on_this_dev_box(self):
        # This function's whole job is locating the real installed Odoo core addons tree --
        # a real dev box with Odoo installed is exactly what it's meant to run against, so
        # asserting against real environment state (not a mock) is the honest test here,
        # matching this codebase's own established convention for filesystem-walk rules
        # (e.g. check_burn_list.py's has_ham_base detection).
        path = orb.find_odoo_core_addons_path()
        self.assertIsNotNone(path)
        self.assertTrue(os.path.isdir(path))


class FindNeededCoreModulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hams_root = os.path.join(self.tmp, "hams_open")
        self.core_addons = os.path.join(self.tmp, "core_addons")
        os.makedirs(self.hams_root)
        os.makedirs(self.core_addons)

    def test_transitive_closure_pulls_in_indirect_core_dependencies(self):
        # ham_qso depends on the core 'website' module; website itself (a real core manifest,
        # read the same way) depends on 'portal' -- portal must be pulled in too even though
        # no hams_open manifest names it directly. ham_events depends on ham_qso, a hams-
        # internal module, which must NOT be treated as a core module needing its own lookup.
        _write(
            os.path.join(self.hams_root, "ham_qso", "__manifest__.py"),
            "{'depends': ['base', 'website']}\n",
        )
        _write(
            os.path.join(self.hams_root, "ham_events", "__manifest__.py"),
            "{'depends': ['ham_qso']}\n",
        )
        _write(
            os.path.join(self.core_addons, "base", "__manifest__.py"),
            "{'depends': []}\n",
        )
        _write(
            os.path.join(self.core_addons, "website", "__manifest__.py"),
            "{'depends': ['portal']}\n",
        )
        _write(
            os.path.join(self.core_addons, "portal", "__manifest__.py"),
            "{'depends': []}\n",
        )
        needed = orb.find_needed_core_modules([self.hams_root], self.core_addons)
        self.assertEqual(needed, {"base", "website", "portal"})

    def test_a_hams_module_named_in_depends_is_never_treated_as_a_core_module(self):
        _write(
            os.path.join(self.hams_root, "ham_events", "__manifest__.py"),
            "{'depends': ['ham_qso']}\n",
        )
        _write(
            os.path.join(self.hams_root, "ham_qso", "__manifest__.py"),
            "{'depends': []}\n",
        )
        needed = orb.find_needed_core_modules([self.hams_root], self.core_addons)
        self.assertEqual(needed, set())


class RegistryBuilderFindSiblingRepoTests(unittest.TestCase):
    # Mirrors the standard sibling-repo-scan regression coverage this session added across
    # every other checker sharing this exact _find_sibling_repo() shape.
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.workspace = os.path.join(self.tmp, "workspace")
        os.makedirs(self.workspace)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _make_repo(self, name, module_names=()):
        repo = os.path.join(self.workspace, name)
        for module_name in module_names:
            _write(os.path.join(repo, module_name, "__manifest__.py"), "{}")
        return repo

    def test_hams_open_finds_a_real_hams_com_sibling(self):
        hams_open = self._make_repo("hams_open", module_names=["zero_sudo"])
        hams_com = self._make_repo("hams_com", module_names=["ham_base"])
        self.assertEqual(orb._find_sibling_repo(hams_open), hams_com)

    def test_no_sibling_directory_present_returns_none(self):
        repo = self._make_repo("hams_open", module_names=["zero_sudo"])
        self.assertIsNone(orb._find_sibling_repo(repo))

    def test_a_sibling_directory_with_no_real_module_in_it_returns_none(self):
        repo = self._make_repo("hams_open", module_names=["zero_sudo"])
        os.makedirs(os.path.join(self.workspace, "hams_com", "not_a_module"))
        self.assertIsNone(orb._find_sibling_repo(repo))


if __name__ == "__main__":
    unittest.main()
