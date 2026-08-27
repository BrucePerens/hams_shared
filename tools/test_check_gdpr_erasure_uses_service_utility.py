#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_gdpr_erasure_uses_service_utility.py.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_gdpr_erasure_uses_service_utility as cge  # noqa: E402


class CheckGdprErasureUsesServiceUtilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, relpath, content):
        path = os.path.join(self.tmp, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_flags_a_hand_rolled_search_then_unlink(self):
        self._write(
            "some_module/models/res_users.py",
            "class ResUsers(models.Model):\n"
            "    def _execute_gdpr_erasure(self):\n"
            "        svc_uid = self.env['zero_sudo.security.utils']._get_service_uid('x.y')\n"
            "        nodes = self.env['some.model'].with_user(svc_uid).search([])\n"
            "        nodes.unlink()\n",
        )
        violations = cge.check_gdpr_erasure_uses_service_utility(self.tmp)
        self.assertEqual(len(violations), 1)
        self.assertIn("res_users.py:5", violations[0])

    def test_flags_a_direct_field_access_unlink(self):
        # ham_repeater_dir's real shape: no explicit .search() call at all,
        # just a relational field access chained straight to .unlink().
        self._write(
            "some_module/models/res_users.py",
            "class ResUsers(models.Model):\n"
            "    def _execute_gdpr_erasure(self):\n"
            "        svc_uid = self.env['zero_sudo.security.utils']._get_service_uid('x.y')\n"
            "        self.repeater_ids.with_user(svc_uid).unlink()\n",
        )
        violations = cge.check_gdpr_erasure_uses_service_utility(self.tmp)
        self.assertEqual(len(violations), 1)

    def test_does_not_flag_a_call_that_uses_the_blessed_utility(self):
        self._write(
            "some_module/models/res_users.py",
            "class ResUsers(models.Model):\n"
            "    def _execute_gdpr_erasure(self):\n"
            "        self.env['zero_sudo.security.utils']._erase_via_service_account(\n"
            "            'some.model', [('user_id', '=', self.id)], 'x.y'\n"
            "        )\n",
        )
        violations = cge.check_gdpr_erasure_uses_service_utility(self.tmp)
        self.assertEqual(violations, [])

    def test_does_not_flag_unlink_calls_outside_execute_gdpr_erasure(self):
        self._write(
            "some_module/models/some_model.py",
            "class SomeModel(models.Model):\n"
            "    def some_other_method(self):\n"
            "        self.env['some.model'].with_user(1).search([]).unlink()\n",
        )
        violations = cge.check_gdpr_erasure_uses_service_utility(self.tmp)
        self.assertEqual(violations, [])

    def test_does_not_flag_a_write_only_erasure_with_no_unlink(self):
        # ham_satellite's real shape: clears fields via write(), no unlink() at all.
        self._write(
            "some_module/models/res_users.py",
            "class ResUsers(models.Model):\n"
            "    def _execute_gdpr_erasure(self):\n"
            "        svc_uid = self.env['zero_sudo.security.utils']._get_service_uid('x.y')\n"
            "        self.with_user(svc_uid).write({'field': False})\n",
        )
        violations = cge.check_gdpr_erasure_uses_service_utility(self.tmp)
        self.assertEqual(violations, [])

    def test_ignore_marker_exempts_a_specific_unlink_call(self):
        self._write(
            "some_module/models/res_users.py",
            "class ResUsers(models.Model):\n"
            "    def _execute_gdpr_erasure(self):\n"
            "        svc_uid = self.env['zero_sudo.security.utils']._get_service_uid('x.y')\n"
            "        nodes = self.env['some.model'].with_user(svc_uid).search([])\n"
            "        nodes.unlink()  # audit-ignore-gdpr-hand-rolled-unlink: batched, see doc\n",
        )
        violations = cge.check_gdpr_erasure_uses_service_utility(self.tmp)
        self.assertEqual(violations, [])

    def test_ignore_marker_on_one_call_does_not_exempt_an_unmarked_sibling_call(self):
        self._write(
            "some_module/models/res_users.py",
            "class ResUsers(models.Model):\n"
            "    def _execute_gdpr_erasure(self):\n"
            "        svc_uid = self.env['zero_sudo.security.utils']._get_service_uid('x.y')\n"
            "        self.env['model.a'].with_user(svc_uid).search([]).unlink()  # audit-ignore-gdpr-hand-rolled-unlink: batched\n"
            "        self.env['model.b'].with_user(svc_uid).search([]).unlink()\n",
        )
        violations = cge.check_gdpr_erasure_uses_service_utility(self.tmp)
        self.assertEqual(len(violations), 1)
        self.assertIn("res_users.py:5", violations[0])

    def test_exempts_security_utils_py_itself(self):
        self._write(
            "zero_sudo/models/security_utils.py",
            "class ZeroSudoSecurityUtils(models.AbstractModel):\n"
            "    def _execute_gdpr_erasure(self):\n"
            "        self.env['some.model'].with_user(1).search([]).unlink()\n",
        )
        violations = cge.check_gdpr_erasure_uses_service_utility(self.tmp)
        self.assertEqual(violations, [])

    def test_a_method_that_both_delegates_and_hand_rolls_unlink_is_flagged_for_the_hand_rolled_call(
        self,
    ):
        # Bruce's explicit decision, 2026-08-27: the guard is now per-call, not whole-method.
        # Before this, `if unignored and not _calls_method_named(node,
        # "_erase_via_service_account")` meant ANY call to _erase_via_service_account
        # anywhere in _execute_gdpr_erasure suppressed EVERY unignored unlink() in that same
        # method, even one on a wholly unrelated model with no relation to the delegated
        # call -- a real gap, since the rule's own escape-hatch design already implied
        # per-call scoping was the intent. Delegating model.a correctly no longer has any
        # suppressing effect on the unrelated, still-hand-rolled model.b unlink().
        self._write(
            "some_module/models/res_users.py",
            "class ResUsers(models.Model):\n"
            "    def _execute_gdpr_erasure(self):\n"
            "        self.env['zero_sudo.security.utils']._erase_via_service_account(\n"
            "            'model.a', [('user_id', '=', self.id)], 'x.y'\n"
            "        )\n"
            "        self.env['model.b'].search([('user_id', '=', self.id)]).unlink()\n",
        )
        violations = cge.check_gdpr_erasure_uses_service_utility(self.tmp)
        self.assertEqual(len(violations), 1)
        self.assertIn("res_users.py:6", violations[0])

    def test_a_method_that_delegates_and_also_uses_the_ignore_marker_is_not_flagged(self):
        # The escape hatch is the only remaining way to exempt a hand-rolled call now that
        # delegating an unrelated model no longer suppresses anything.
        self._write(
            "some_module/models/res_users.py",
            "class ResUsers(models.Model):\n"
            "    def _execute_gdpr_erasure(self):\n"
            "        self.env['zero_sudo.security.utils']._erase_via_service_account(\n"
            "            'model.a', [('user_id', '=', self.id)], 'x.y'\n"
            "        )\n"
            "        self.env['model.b'].search([('user_id', '=', self.id)]).unlink()"
            "  # audit-ignore-gdpr-hand-rolled-unlink: batched, see docstring\n",
        )
        violations = cge.check_gdpr_erasure_uses_service_utility(self.tmp)
        self.assertEqual(violations, [])

    def test_a_file_with_invalid_utf8_bytes_is_skipped_with_a_warning_not_a_crash(self):
        path = self._write("some_module/models/broken_encoding.py", "")
        with open(path, "wb") as f:
            f.write(b"# -*- coding: utf-8 -*-\nx = '\xff\xfe broken bytes'\n")
        violations = cge.check_gdpr_erasure_uses_service_utility(self.tmp)
        self.assertEqual(violations, [])

    def test_a_syntax_broken_file_is_skipped_without_crashing(self):
        self._write(
            "some_module/models/broken_syntax.py",
            "class ResUsers(models.Model:\n"
            "    def _execute_gdpr_erasure(self):\n"
            "        self.env['model.a'].search([]).unlink()\n",
        )
        violations = cge.check_gdpr_erasure_uses_service_utility(self.tmp)
        self.assertEqual(violations, [])

    def test_a_non_python_file_alongside_a_real_violation_is_skipped_not_crashed_on(self):
        self._write("some_module/models/README.md", "# notes\n")
        self._write(
            "some_module/models/res_users.py",
            "class ResUsers(models.Model):\n"
            "    def _execute_gdpr_erasure(self):\n"
            "        self.env['model.a'].search([]).unlink()\n",
        )
        violations = cge.check_gdpr_erasure_uses_service_utility(self.tmp)
        self.assertEqual(len(violations), 1)


class ResolveRepoRootTests(unittest.TestCase):
    def test_a_hams_shared_path_redirects_to_its_parent_repo(self):
        fake_repo = os.path.join(os.sep, "some", "workspace", "some_repo")
        self.assertEqual(
            cge._resolve_repo_root(os.path.join(fake_repo, "hams_shared")),
            fake_repo,
        )

    def test_a_real_repo_root_passes_through_unchanged(self):
        fake_repo = os.path.join(os.sep, "some", "workspace", "some_repo")
        self.assertEqual(cge._resolve_repo_root(fake_repo), fake_repo)


class ResolveRepoRootsTests(unittest.TestCase):
    # Regression test for the second half of the same bug class, found 2026-08-27:
    # _resolve_repo_root's redirect only ever lands on ONE repo (hams_shared's literal parent) --
    # but real _execute_gdpr_erasure() overrides exist in both repos (17 files in hams_com, 7 in
    # hams_open), so the single-repo redirect left the larger half unscanned via run_linters.py's
    # actual invocation.
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.workspace = os.path.join(self.tmp, "workspace")
        os.makedirs(self.workspace)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_repo(self, name, module_names=()):
        repo = os.path.join(self.workspace, name)
        for module_name in module_names:
            manifest_path = os.path.join(repo, module_name, "__manifest__.py")
            os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
            with open(manifest_path, "w", encoding="utf-8") as f:
                f.write("{}")
        return repo

    def test_hams_shared_input_appends_the_real_hams_com_sibling(self):
        self._make_repo("hams_open", module_names=["zero_sudo"])
        hams_com = self._make_repo("hams_com", module_names=["ham_base"])
        hams_shared = os.path.join(self.workspace, "hams_open", "hams_shared")
        os.makedirs(hams_shared)
        roots = cge._resolve_repo_roots(hams_shared)
        self.assertEqual(roots, [os.path.join(self.workspace, "hams_open"), hams_com])

    def test_a_real_repo_root_with_no_odoo_sibling_present_scans_alone(self):
        repo = self._make_repo("hams_open", module_names=["zero_sudo"])
        self.assertEqual(cge._resolve_repo_roots(repo), [repo])

    def test_a_sibling_directory_with_no_manifest_py_anywhere_is_not_treated_as_a_repo(self):
        repo = self._make_repo("hams_open", module_names=["zero_sudo"])
        os.makedirs(os.path.join(self.workspace, "hams_com", "not_a_module"))
        self.assertEqual(cge._resolve_repo_roots(repo), [repo])


if __name__ == "__main__":
    unittest.main()
