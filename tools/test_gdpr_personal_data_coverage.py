#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for gdpr_personal_data_coverage.py."""

import os
import shutil
import tempfile
import unittest

import gdpr_personal_data_coverage as cov

MODEL = (
    "from odoo import fields, models\n"
    "class Thing(models.Model):\n"
    "    _name = 'x.thing'\n"
    "    user_id = fields.Many2one('res.users', ondelete='cascade')\n"
    "    note = fields.Char()\n"
)


class GdprPersonalDataCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, relpath, content):
        path = os.path.join(self.tmp, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)

    def test_a_model_with_a_user_link_and_no_hook_is_flagged(self):
        self._write("mod/models/thing.py", MODEL)
        rows = cov.coverage([self.tmp])
        self.assertEqual([r[0] for r in cov.flagged(rows)], ["x.thing"])
        self.assertEqual(rows[0][2], ["user_id->res.users(cascade)"])

    def test_naming_the_model_in_an_export_hook_file_covers_it(self):
        self._write("mod/models/thing.py", MODEL)
        self._write("mod/models/res_users.py", "def _get_gdpr_export_data(self):\n    return {'k': 'x.thing'}\n")
        rows = cov.coverage([self.tmp])
        self.assertEqual(cov.flagged(rows), [])
        self.assertTrue(rows[0][3] and not rows[0][4])

    def test_naming_the_model_in_an_erasure_hook_file_covers_it(self):
        self._write("mod/models/thing.py", MODEL)
        self._write("mod/models/res_users.py", "def _execute_gdpr_erasure(self):\n    erase('x.thing')\n")
        rows = cov.coverage([self.tmp])
        self.assertEqual(cov.flagged(rows), [])
        self.assertTrue(rows[0][4] and not rows[0][3])

    def test_a_reviewed_model_is_not_flagged(self):
        self._write("mod/models/thing.py", MODEL)
        rows = cov.coverage([self.tmp], {"x.thing": "kept by decision"})
        self.assertEqual(cov.flagged(rows), [])

    def test_wizards_sql_views_and_models_without_personal_fields_are_skipped(self):
        self._write(
            "mod/models/a.py",
            "class W(models.TransientModel):\n    _name = 'x.wiz'\n    user_id = fields.Many2one('res.users')\n"
            "class V(models.Model):\n    _name = 'x.view'\n    _auto = False\n    email = fields.Char()\n"
            "class P(models.Model):\n    _name = 'x.plain'\n    title = fields.Char()\n",
        )
        self.assertEqual(cov.coverage([self.tmp]), [])

    def test_personal_value_fields_are_found_by_name(self):
        self._write("mod/models/a.py", "class P(models.Model):\n    _name = 'x.addr'\n    street = fields.Char()\n    ip_address = fields.Char()\n")
        self.assertEqual(cov.coverage([self.tmp])[0][2], ["ip_address:Char", "street:Char"])

    def test_inherit_adds_fields_to_the_named_model(self):
        self._write("mod/models/a.py", "class P(models.Model):\n    _inherit = 'res.partner'\n    club_user_id = fields.Many2one('res.users')\n")
        self.assertEqual([r[0] for r in cov.coverage([self.tmp])], ["res.partner"])

    def test_strict_mode_exits_nonzero_only_when_something_is_flagged(self):
        self._write("mod/models/thing.py", MODEL)
        self.assertEqual(cov.main(["--strict", self.tmp]), 1)
        self.assertEqual(cov.main([self.tmp]), 0)


if __name__ == "__main__":
    unittest.main()
