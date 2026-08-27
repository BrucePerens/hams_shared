#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_self_writeable_field_tests.py (MASTER_10 SELF_WRITEABLE_FIELDS
write-proof verifier).
"""

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_self_writeable_field_tests as chk  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_self_writeable_field_tests.py")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _func_node(src, name=None):
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and (name is None or node.name == name):
            return node
    raise AssertionError("no matching function found in fixture source")


class OwningModuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_the_nearest_ancestor_manifest_directory(self):
        _write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        target = os.path.join(self.tmp, "mod_a", "models", "res_users.py")
        _write(target, "# empty\n")
        self.assertEqual(chk._owning_module(target, self.tmp), os.path.join(self.tmp, "mod_a"))

    def test_returns_none_when_no_ancestor_has_a_manifest(self):
        target = os.path.join(self.tmp, "not_a_module", "models", "res_users.py")
        _write(target, "# empty\n")
        self.assertIsNone(chk._owning_module(target, self.tmp))

    def test_returns_none_when_repo_root_equals_the_files_own_directory(self):
        # Documents a real edge case: the loop's `d != repo_root` guard is
        # false on the very first check when repo_root is the file's own
        # starting directory, so this always returns None regardless of a
        # real ancestor manifest existing -- callers must pass a real
        # ancestor above the file, not its immediate directory. main() got
        # this wrong once (called with os.path.dirname(path) as repo_root,
        # always returning None) before being fixed to try each real root
        # in `roots` instead.
        _write(os.path.join(self.tmp, "__manifest__.py"), "{}\n")
        target = os.path.join(self.tmp, "models.py")
        self.assertIsNone(chk._owning_module(target, os.path.dirname(target)))


class FindSelfWriteableOverridesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_an_override_with_an_anchor(self):
        _write(
            os.path.join(self.tmp, "mod_a", "models", "res_users.py"),
            "class ResUsers(models.Model):\n"
            "    _inherit = 'res.users'\n\n"
            "    @property\n"
            "    # Verified by [@ANCHOR: mod_a:self_write]\n"
            "    def SELF_WRITEABLE_FIELDS(self):\n"
            "        return super().SELF_WRITEABLE_FIELDS + ['callsign']\n",
        )
        found = list(chk._find_self_writeable_overrides([self.tmp]))
        self.assertEqual(len(found), 1)
        _path, anchor, _lineno = found[0]
        self.assertEqual(anchor, "mod_a:self_write")

    def test_an_override_with_no_anchor_comment_yields_none_anchor(self):
        _write(
            os.path.join(self.tmp, "mod_a", "models", "res_users.py"),
            "class ResUsers(models.Model):\n"
            "    def SELF_WRITEABLE_FIELDS(self):\n"
            "        return []\n",
        )
        found = list(chk._find_self_writeable_overrides([self.tmp]))
        self.assertEqual(len(found), 1)
        self.assertIsNone(found[0][1])

    def test_ignores_overrides_that_live_inside_a_tests_directory(self):
        _write(
            os.path.join(self.tmp, "mod_a", "tests", "test_something.py"),
            "class T(TransactionCase):\n"
            "    def SELF_WRITEABLE_FIELDS(self):\n"
            "        return []\n",
        )
        found = list(chk._find_self_writeable_overrides([self.tmp]))
        self.assertEqual(found, [])

    def test_no_override_anywhere_yields_nothing(self):
        _write(os.path.join(self.tmp, "mod_a", "models", "res_users.py"), "class Foo:\n    pass\n")
        self.assertEqual(list(chk._find_self_writeable_overrides([self.tmp])), [])

    def test_a_radae_directory_is_never_walked(self):
        # radae is excluded from directory traversal same as node_modules/tools/daemons --
        # even a real override sitting inside it must never be found.
        _write(
            os.path.join(self.tmp, "radae", "models", "res_users.py"),
            "class ResUsers(models.Model):\n"
            "    def SELF_WRITEABLE_FIELDS(self):\n"
            "        return []\n",
        )
        self.assertEqual(list(chk._find_self_writeable_overrides([self.tmp])), [])

    def test_a_non_python_file_alongside_a_real_override_is_skipped_not_crashed_on(self):
        _write(os.path.join(self.tmp, "mod_a", "models", "README.md"), "# notes\n")
        _write(
            os.path.join(self.tmp, "mod_a", "models", "res_users.py"),
            "class ResUsers(models.Model):\n"
            "    def SELF_WRITEABLE_FIELDS(self):\n"
            "        return []\n",
        )
        found = list(chk._find_self_writeable_overrides([self.tmp]))
        self.assertEqual(len(found), 1)


class FindTestsAnchorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.module_dir = os.path.join(self.tmp, "mod_a")
        os.makedirs(self.module_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_a_method_level_anchor(self):
        # A comment on the line immediately above `def` is NOT part of the
        # FunctionDef node's own lineno..end_lineno range (comments aren't
        # AST nodes, and the range starts at the def/decorator line) -- the
        # anchor has to sit inside the function body to land as a
        # method-level match rather than falling back to class-level.
        _write(
            os.path.join(self.module_dir, "tests", "test_self_write.py"),
            "class T(TransactionCase):\n"
            "    def test_self_write_works(self):\n"
            "        # Tests [@ANCHOR: mod_a:self_write]\n"
            "        pass\n",
        )
        path, node, _content = chk._find_tests_anchor(self.module_dir, "mod_a:self_write")
        self.assertIsNotNone(node)
        self.assertEqual(node.name, "test_self_write_works")

    def test_falls_back_to_a_bare_anchor_form(self):
        # The "elaborate form" this codebase also uses: a bare
        # [@ANCHOR: X] on the test side, not a "# Tests [@ANCHOR: X]" line.
        _write(
            os.path.join(self.module_dir, "tests", "test_self_write.py"),
            "class T(TransactionCase):\n"
            "    def test_self_write_works(self):\n"
            "        # [@ANCHOR: mod_a:self_write]\n"
            "        pass\n",
        )
        _path, node, _content = chk._find_tests_anchor(self.module_dir, "mod_a:self_write")
        self.assertIsNotNone(node)
        self.assertEqual(node.name, "test_self_write_works")

    def test_falls_back_to_class_level_when_no_method_contains_the_anchor(self):
        _write(
            os.path.join(self.module_dir, "tests", "test_self_write.py"),
            "class T(TransactionCase):\n"
            "    # Tests [@ANCHOR: mod_a:self_write]\n"
            "    def test_a(self):\n"
            "        pass\n"
            "    def test_b(self):\n"
            "        pass\n",
        )
        _path, node, _content = chk._find_tests_anchor(self.module_dir, "mod_a:self_write")
        self.assertIsNotNone(node)
        self.assertIsInstance(node, ast.ClassDef)

    def test_returns_none_when_the_module_has_no_tests_directory(self):
        result = chk._find_tests_anchor(self.module_dir, "mod_a:self_write")
        self.assertEqual(result, (None, None, None))

    def test_returns_none_when_no_matching_anchor_exists(self):
        _write(
            os.path.join(self.module_dir, "tests", "test_self_write.py"),
            "class T(TransactionCase):\n"
            "    # Tests [@ANCHOR: mod_a:something_else]\n"
            "    def test_a(self):\n"
            "        pass\n",
        )
        result = chk._find_tests_anchor(self.module_dir, "mod_a:self_write")
        self.assertEqual(result, (None, None, None))

    def test_a_file_with_a_matching_anchor_but_broken_syntax_is_skipped_not_crashed_on(self):
        _write(
            os.path.join(self.module_dir, "tests", "test_broken.py"),
            "class T(TransactionCase:\n"
            "    # Tests [@ANCHOR: mod_a:self_write]\n"
            "    def test_a(self):\n"
            "        pass\n",
        )
        result = chk._find_tests_anchor(self.module_dir, "mod_a:self_write")
        self.assertEqual(result, (None, None, None))

    def test_a_non_python_file_in_the_tests_directory_is_skipped(self):
        _write(os.path.join(self.module_dir, "tests", "README.md"), "# notes\n")
        _write(
            os.path.join(self.module_dir, "tests", "test_self_write.py"),
            "class T(TransactionCase):\n"
            "    def test_self_write_works(self):\n"
            "        # Tests [@ANCHOR: mod_a:self_write]\n"
            "        pass\n",
        )
        path, node, _content = chk._find_tests_anchor(self.module_dir, "mod_a:self_write")
        self.assertIsNotNone(path)
        self.assertEqual(node.name, "test_self_write_works")


class VerifyWriteProofShapeTests(unittest.TestCase):
    def test_a_complete_write_proof_has_no_errors(self):
        node = _func_node(
            "def test_self_write_works(self):\n"
            "    user = self.env['res.users'].create({'name': 'x', 'login': 'x'})\n"
            "    user.with_user(user).write({'callsign': 'K6BP'})\n"
            "    self.assertEqual(user.callsign, 'K6BP')\n"
        )
        self.assertEqual(chk._verify_write_proof_shape(node), [])

    def test_missing_write_call_is_flagged(self):
        node = _func_node(
            "def test_self_write_works(self):\n"
            "    user.with_user(user)\n"
            "    self.assertTrue(True)\n"
        )
        errors = chk._verify_write_proof_shape(node)
        self.assertTrue(any("does not call .write" in e for e in errors))

    def test_missing_with_user_call_is_flagged(self):
        node = _func_node(
            "def test_self_write_works(self):\n"
            "    user.write({'callsign': 'K6BP'})\n"
            "    self.assertEqual(user.callsign, 'K6BP')\n"
        )
        errors = chk._verify_write_proof_shape(node)
        self.assertTrue(any("does not call .with_user" in e for e in errors))

    def test_write_with_no_assertion_anywhere_is_flagged(self):
        node = _func_node(
            "def test_self_write_works(self):\n"
            "    user.with_user(user).write({'callsign': 'K6BP'})\n"
        )
        errors = chk._verify_write_proof_shape(node)
        self.assertTrue(any("no assertEqual" in e for e in errors))

    def test_an_assertion_before_the_write_does_not_count_as_proof(self):
        # An assertion textually before .write() proves nothing about the
        # write's own effect -- must be an assertion AFTER it.
        node = _func_node(
            "def test_self_write_works(self):\n"
            "    self.assertTrue(True)\n"
            "    user.with_user(user).write({'callsign': 'K6BP'})\n"
        )
        errors = chk._verify_write_proof_shape(node)
        self.assertTrue(any("no assertEqual" in e for e in errors))

    def test_a_write_nested_in_a_try_block_is_still_found(self):
        # ast.walk() is breadth-first, not source order -- this is the
        # exact case the function's own comment documents needing a
        # two-pass line-number comparison, not a mid-walk one, to get right.
        node = _func_node(
            "def test_self_write_works(self):\n"
            "    try:\n"
            "        user.with_user(user).write({'callsign': 'K6BP'})\n"
            "    except Exception:\n"
            "        pass\n"
            "    self.assertEqual(user.callsign, 'K6BP')\n"
        )
        self.assertEqual(chk._verify_write_proof_shape(node), [])


class MainIntegrationTests(unittest.TestCase):
    """Runs the script as a real subprocess against temp-directory module
    fixtures, exercising main()'s own module-resolution and wiring path."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        result = subprocess.run(
            [sys.executable, _SCRIPT, self.tmp], capture_output=True, text=True, timeout=30
        )
        return result.returncode, result.stdout + result.stderr

    def test_a_fully_verified_override_passes(self):
        _write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        _write(
            os.path.join(self.tmp, "mod_a", "models", "res_users.py"),
            "class ResUsers(models.Model):\n"
            "    _inherit = 'res.users'\n\n"
            "    # Verified by [@ANCHOR: mod_a:self_write]\n"
            "    def SELF_WRITEABLE_FIELDS(self):\n"
            "        return super().SELF_WRITEABLE_FIELDS + ['callsign']\n",
        )
        _write(
            os.path.join(self.tmp, "mod_a", "tests", "test_self_write.py"),
            "class T(TransactionCase):\n"
            "    # Tests [@ANCHOR: mod_a:self_write]\n"
            "    def test_self_write_works(self):\n"
            "        user.with_user(user).write({'callsign': 'K6BP'})\n"
            "        self.assertEqual(user.callsign, 'K6BP')\n",
        )
        code, out = self._run()
        self.assertEqual(code, 0, out)

    def test_an_override_with_no_anchor_fails(self):
        _write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        _write(
            os.path.join(self.tmp, "mod_a", "models", "res_users.py"),
            "class ResUsers(models.Model):\n"
            "    def SELF_WRITEABLE_FIELDS(self):\n"
            "        return []\n",
        )
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("no", out)
        self.assertIn("ANCHOR", out)

    def test_an_anchor_with_no_matching_test_fails(self):
        _write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        _write(
            os.path.join(self.tmp, "mod_a", "models", "res_users.py"),
            "class ResUsers(models.Model):\n"
            "    # Verified by [@ANCHOR: mod_a:self_write]\n"
            "    def SELF_WRITEABLE_FIELDS(self):\n"
            "        return []\n",
        )
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("no matching", out)

    def test_a_hollow_test_that_never_writes_fails(self):
        _write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        _write(
            os.path.join(self.tmp, "mod_a", "models", "res_users.py"),
            "class ResUsers(models.Model):\n"
            "    # Verified by [@ANCHOR: mod_a:self_write]\n"
            "    def SELF_WRITEABLE_FIELDS(self):\n"
            "        return []\n",
        )
        _write(
            os.path.join(self.tmp, "mod_a", "tests", "test_self_write.py"),
            "class T(TransactionCase):\n"
            "    # Tests [@ANCHOR: mod_a:self_write]\n"
            "    def test_self_write_works(self):\n"
            "        fields = self.env['res.users'].SELF_WRITEABLE_FIELDS\n"
            "        self.assertIn('callsign', fields)\n",
        )
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("does not prove the self-write actually works", out)


class ResolveRepoRootTests(unittest.TestCase):
    # Regression test for a real bug found the same night check_model_extension_collisions.py's
    # own identical bug was found and fixed: run_linters.py's own dir_path resolves to
    # .../hams_shared, not a real repo root -- confirmed directly, this checker was silently
    # finding 0 SELF_WRITEABLE_FIELDS overrides via run_linters.py's actual invocation, versus 8
    # at a real repo root, one of which (ham_callbook's) was a genuine, previously-undetected
    # MASTER_10 write-proof gap.
    def test_a_hams_shared_path_redirects_to_its_parent_repo(self):
        fake_repo = os.path.join(os.sep, "some", "workspace", "some_repo")
        self.assertEqual(
            chk._resolve_repo_root(os.path.join(fake_repo, "hams_shared")),
            fake_repo,
        )

    def test_a_real_repo_root_passes_through_unchanged(self):
        fake_repo = os.path.join(os.sep, "some", "workspace", "some_repo")
        self.assertEqual(chk._resolve_repo_root(fake_repo), fake_repo)


class ResolveRepoRootsTests(unittest.TestCase):
    # This is the reference checker that already had the correct sibling-repo scan built in from
    # the start -- exactly why it was the one that found ham_callbook's real, previously-
    # undetected MASTER_10 gap when the other 8 checkers using this same _resolve_repo_root-only
    # pattern were still silently missing hams_com entirely. Never had its own regression test
    # for this specific behavior until the 2026-08-27 pass that added the identical test to the
    # 8 other checkers this one was the template for.
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.workspace = os.path.join(self.tmp, "workspace")
        os.makedirs(self.workspace)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_repo(self, name, module_names=()):
        repo = os.path.join(self.workspace, name)
        for module_name in module_names:
            _write(os.path.join(repo, module_name, "__manifest__.py"), "{}")
        return repo

    def test_hams_shared_input_appends_the_real_hams_com_sibling(self):
        self._make_repo("hams_open", module_names=["zero_sudo"])
        hams_com = self._make_repo("hams_com", module_names=["ham_base"])
        hams_shared = os.path.join(self.workspace, "hams_open", "hams_shared")
        os.makedirs(hams_shared)
        roots = chk._resolve_repo_roots(hams_shared)
        self.assertEqual(roots, [os.path.join(self.workspace, "hams_open"), hams_com])

    def test_a_real_repo_root_with_no_odoo_sibling_present_scans_alone(self):
        repo = self._make_repo("hams_open", module_names=["zero_sudo"])
        self.assertEqual(chk._resolve_repo_roots(repo), [repo])

    def test_a_sibling_directory_with_no_manifest_py_anywhere_is_not_treated_as_a_repo(self):
        repo = self._make_repo("hams_open", module_names=["zero_sudo"])
        os.makedirs(os.path.join(self.workspace, "hams_com", "not_a_module"))
        self.assertEqual(chk._resolve_repo_roots(repo), [repo])


if __name__ == "__main__":
    unittest.main()
