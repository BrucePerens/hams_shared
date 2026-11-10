#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for check_settings_secrets.py."""

import os
import tempfile
import unittest

import check_settings_secrets as css  # noqa: E402

HEAD = "from odoo import fields, models\n\nclass S(models.TransientModel):\n    _inherit = 'res.config.settings'\n"


def keys(source):
    return [key.split(":", 1)[1] for key, _ in css.scan_source(source, "m.py")]


class ScanTests(unittest.TestCase):
    def test_an_unmasked_secret_named_field_is_found(self):
        self.assertEqual(keys(HEAD + "    shop_secret = fields.Char()\n"), ["S.shop_secret"])

    def test_a_secret_looking_config_parameter_is_found_even_with_a_plain_field_name(self):
        self.assertEqual(keys(HEAD + "    shop = fields.Char(config_parameter='x.api_key')\n"), ["S.shop"])

    def test_the_seed_parameters_of_the_hams1_inventory_are_found(self):
        for parameter in sorted(css.SEED_PARAMETERS):
            self.assertEqual(keys(HEAD + "    shop = fields.Char(config_parameter=%r)\n" % parameter), ["S.shop"], parameter)

    def test_a_field_named_in_get_values_is_masked(self):
        source = HEAD + "    shop_secret = fields.Char()\n    def get_values(self):\n        res = super().get_values()\n        res['shop_secret'] = ''\n        return res\n"
        self.assertEqual(keys(source), [])

    def test_a_get_values_that_does_not_name_the_field_does_not_mask_it(self):
        source = HEAD + "    shop_secret = fields.Char()\n    def get_values(self):\n        return super().get_values()\n"
        self.assertEqual(keys(source), ["S.shop_secret"])

    def test_public_keys_flags_and_non_secrets_are_not_found(self):
        source = HEAD + "    a = fields.Char(config_parameter='mail.web_push_vapid_public_key')\n    b = fields.Char(config_parameter='auth_signup.reset_password')\n    c = fields.Char()\n    d = fields.Boolean(config_parameter='x.secret')\n"
        self.assertEqual(keys(source), [])

    def test_the_exemption_comment_needs_a_reason_marker_on_the_field(self):
        self.assertEqual(keys(HEAD + "    token_url = fields.Char()  # settings-secret-ok: a URL\n"), [])

    def test_a_class_that_is_not_a_settings_model_is_ignored(self):
        self.assertEqual(keys("from odoo import fields, models\n\nclass M(models.Model):\n    _name = 'm'\n    shop_secret = fields.Char()\n"), [])


class RepoTests(unittest.TestCase):
    def test_compare_fails_on_new_and_notes_fixed(self):
        problems, notes = css.compare({"a": 1, "b": 2}, {"b": {}, "c": {}})
        self.assertEqual((len(problems), len(notes)), (1, 1))
        self.assertIn("a:", problems[0] + ":")

    def test_the_baseline_cannot_grow_once_it_exists(self):
        with self.assertRaises(ValueError):
            css.render_baseline({"a": 1, "b": 2}, {"a": {"owner": "o", "tracking": "t"}})

    def test_scan_repo_skips_tests_directories(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "mod", "tests"))
            for sub in ("mod", os.path.join("mod", "tests")):
                with open(os.path.join(root, sub, "s.py"), "w", encoding="utf-8") as handle:
                    handle.write(HEAD + "    shop_secret = fields.Char()\n")
            self.assertEqual(list(css.scan_repo(root)), [os.path.join("mod", "s.py") + ":S.shop_secret"])

    def test_main_exit_codes(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "s.py"), "w", encoding="utf-8") as handle:
                handle.write(HEAD + "    shop_secret = fields.Char()\n")
            self.assertEqual(css.main([root]), 1)
            baseline = os.path.join(root, "b.json")
            self.assertEqual(css.main([root, "--baseline", baseline, "--write-baseline"]), 0)
            self.assertEqual(css.main([root, "--baseline", baseline]), 0)


if __name__ == "__main__":
    unittest.main()
