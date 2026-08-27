#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_registry_test_cr_usage.py.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_registry_test_cr_usage as crtc  # noqa: E402


class CheckRegistryTestCrUsageTests(unittest.TestCase):
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

    def test_flags_the_real_broken_idiom(self):
        self._write(
            "some_module/models/res_users.py",
            "def _execute_gdpr_erasure(self):\n"
            "    is_test = vars(self.env.registry).get('test_cr') is not None\n",
        )
        violations = crtc.check_registry_test_cr_usage(self.tmp)
        self.assertEqual(len(violations), 1)
        self.assertIn("res_users.py:2", violations[0])

    def test_flags_the_double_quoted_spelling_too(self):
        self._write(
            "some_module/models/res_users.py",
            'is_test = vars(self.env.registry).get("test_cr") is not None\n',
        )
        violations = crtc.check_registry_test_cr_usage(self.tmp)
        self.assertEqual(len(violations), 1)

    def test_does_not_flag_unrelated_registry_or_test_cr_usage(self):
        self._write(
            "some_module/models/res_users.py",
            "def foo(self):\n"
            "    registry = self.env.registry\n"
            "    test_cr = self.env.cr\n"
            "    return registry, test_cr\n",
        )
        violations = crtc.check_registry_test_cr_usage(self.tmp)
        self.assertEqual(violations, [])

    def test_clean_file_produces_no_violations(self):
        self._write(
            "some_module/models/res_users.py",
            "def _execute_gdpr_erasure(self):\n"
            "    nodes = self.env['ham.relay.node'].search([])\n"
            "    nodes.unlink()\n",
        )
        violations = crtc.check_registry_test_cr_usage(self.tmp)
        self.assertEqual(violations, [])


class ResolveRepoRootTests(unittest.TestCase):
    def test_a_hams_shared_path_redirects_to_its_parent_repo(self):
        fake_repo = os.path.join(os.sep, "some", "workspace", "some_repo")
        self.assertEqual(
            crtc._resolve_repo_root(os.path.join(fake_repo, "hams_shared")),
            fake_repo,
        )

    def test_a_real_repo_root_passes_through_unchanged(self):
        fake_repo = os.path.join(os.sep, "some", "workspace", "some_repo")
        self.assertEqual(crtc._resolve_repo_root(fake_repo), fake_repo)


class ResolveRepoRootsTests(unittest.TestCase):
    # Regression test for the second half of the same bug class, found 2026-08-27:
    # _resolve_repo_root's redirect only ever lands on ONE repo (hams_shared's literal parent) --
    # but real Odoo Python source spans both hams_open and hams_com, so the single-repo redirect
    # left hams_com entirely unscanned via run_linters.py's actual invocation.
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
        roots = crtc._resolve_repo_roots(hams_shared)
        self.assertEqual(roots, [os.path.join(self.workspace, "hams_open"), hams_com])

    def test_a_real_repo_root_with_no_odoo_sibling_present_scans_alone(self):
        repo = self._make_repo("hams_open", module_names=["zero_sudo"])
        self.assertEqual(crtc._resolve_repo_roots(repo), [repo])

    def test_a_sibling_directory_with_no_manifest_py_anywhere_is_not_treated_as_a_repo(self):
        repo = self._make_repo("hams_open", module_names=["zero_sudo"])
        os.makedirs(os.path.join(self.workspace, "hams_com", "not_a_module"))
        self.assertEqual(crtc._resolve_repo_roots(repo), [repo])


if __name__ == "__main__":
    unittest.main()
