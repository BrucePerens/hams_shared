#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_xml_comment_double_hyphen.py.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_xml_comment_double_hyphen as cxc  # noqa: E402


class CheckXmlCommentDoubleHyphenTests(unittest.TestCase):
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

    def test_flags_an_em_dash_style_double_hyphen(self):
        self._write(
            "some_module/security/security_rules.xml",
            "<odoo>\n"
            "<!-- some explanation -- with an aside -->\n"
            "<record/>\n"
            "</odoo>\n",
        )
        violations = cxc.check_xml_comment_double_hyphen(self.tmp)
        self.assertEqual(len(violations), 1)
        self.assertIn("security_rules.xml:2", violations[0])

    def test_flags_a_multiline_comment_double_hyphen(self):
        self._write(
            "some_module/views/some_views.xml",
            "<odoo>\n"
            "<!-- line one is fine\n"
            "     line two has a -- double hyphen\n"
            "     line three is fine -->\n"
            "</odoo>\n",
        )
        violations = cxc.check_xml_comment_double_hyphen(self.tmp)
        self.assertEqual(len(violations), 1)
        self.assertIn("some_views.xml:3", violations[0])

    def test_does_not_flag_a_clean_comment(self):
        self._write(
            "some_module/security/security_rules.xml",
            "<odoo>\n"
            "<!-- some explanation, with an aside -->\n"
            "<record/>\n"
            "</odoo>\n",
        )
        violations = cxc.check_xml_comment_double_hyphen(self.tmp)
        self.assertEqual(violations, [])

    def test_does_not_flag_double_hyphens_outside_comments(self):
        self._write(
            "some_module/views/some_views.xml",
            '<odoo>\n<field name="domain">[(1, \'=\', 1)]</field>\n</odoo>\n',
        )
        violations = cxc.check_xml_comment_double_hyphen(self.tmp)
        self.assertEqual(violations, [])

    def test_ignores_non_xml_files(self):
        self._write("some_module/models/foo.py", "# some comment -- with an aside\n")
        violations = cxc.check_xml_comment_double_hyphen(self.tmp)
        self.assertEqual(violations, [])


class ResolveRepoRootTests(unittest.TestCase):
    def test_a_hams_shared_path_redirects_to_its_parent_repo(self):
        fake_repo = os.path.join(os.sep, "some", "workspace", "some_repo")
        self.assertEqual(
            cxc._resolve_repo_root(os.path.join(fake_repo, "hams_shared")),
            fake_repo,
        )

    def test_a_real_repo_root_passes_through_unchanged(self):
        fake_repo = os.path.join(os.sep, "some", "workspace", "some_repo")
        self.assertEqual(cxc._resolve_repo_root(fake_repo), fake_repo)


if __name__ == "__main__":
    unittest.main()
