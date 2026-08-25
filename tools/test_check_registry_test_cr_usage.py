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


if __name__ == "__main__":
    unittest.main()
