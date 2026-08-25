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


if __name__ == "__main__":
    unittest.main()
