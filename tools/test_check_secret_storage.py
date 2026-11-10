#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for check_secret_storage.py: each rule with a failing fixture and a passing one, the ignore comment, the known
list (an entry silences its finding; a stale entry fails), and the command line."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_secret_storage as chk  # noqa: E402


def _write(directory, name, content):
    path = os.path.join(directory, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(textwrap.dedent(content))
    return path


class SecretStorageTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def findings(self, content, name="mod/models/settings.py"):
        return chk.check_file(_write(self.tmp, name, content))


class NamesTests(SecretStorageTestCase):
    def test_secret_names(self):
        # Tests [@ANCHOR: secret_storage:names_a_secret]
        for name in ("stripe.secret_key", "redis_password", "gemini.api_key", "acme_token", "ham_map.aprs_credential",
                     "form_shield.signing_key", "mail.web_push_vapid_private_key", "smtp_passwd", "x.apikey"):
            self.assertTrue(chk.names_a_secret(name), name)
        for name in ("mail.web_push_vapid_public_key", "auth_signup.reset_password", "token_expiry", "api_key_set",
                     "secret_key_hash", "stripe.publishable_key", "password_policy", "token_url", "api_key_configured",
                     "partner_id", "keyboard_layout"):
            self.assertFalse(chk.names_a_secret(name), name)

    def test_test_paths(self):
        # Tests [@ANCHOR: secret_storage:is_test_path]
        for path in ("mod/tests/test_x.py", "tools/test_y.py", "a/b_test.py", "conftest.py"):
            self.assertTrue(chk.is_test_path(path), path)
        self.assertFalse(chk.is_test_path("mod/models/x.py"))


class ConfigParameterRuleTests(SecretStorageTestCase):
    def test_set_param_of_a_secret_fails(self):
        # Tests [@ANCHOR: secret_storage:check_python]
        # Tests [@ANCHOR: secret_storage:call_findings]
        found = self.findings("""
            def post_init(env):
                env["ir.config_parameter"].set_param("stripe.secret_key", "sk")
                env["ir.config_parameter"].set_param("stripe.publishable_key", "pk")
                env["ir.config_parameter"].set_param(dynamic_key, "v")
        """)
        self.assertEqual([(f[0], f[1]) for f in found], [("secret-config-parameter", "stripe.secret_key")])

    def test_config_parameter_field_fails(self):
        found = self.findings("""
            class ResConfigSettings(models.TransientModel):
                _inherit = "res.config.settings"
                redis_pw = fields.Char(config_parameter="cache.redis_password")
                redis_host = fields.Char(config_parameter="cache.redis_host")
        """)
        self.assertEqual([(f[0], f[1]) for f in found], [("secret-config-parameter", "cache.redis_password")])

    def test_xml_record_fails(self):
        # Tests [@ANCHOR: secret_storage:check_xml]
        found = self.findings("""
            <odoo>
              <record id="p1" model="ir.config_parameter">
                <field name="key">parking.form_secret</field>
                <field name="value">x</field>
              </record>
              <record id="p2" model="ir.config_parameter">
                <field name="key">parking.page_size</field>
              </record>
            </odoo>
        """, "mod/data/params.xml")
        self.assertEqual([(f[0], f[1], f[2]) for f in found], [("secret-config-parameter", "parking.form_secret", 4)])

    def test_ignore_comment_needs_the_rule_and_a_reason(self):
        # Tests [@ANCHOR: secret_storage:silenced]
        found = self.findings("""
            def a(env):
                env["ir.config_parameter"].set_param("x.token", "1")  # secret-storage-ignore: secret-config-parameter: a public demo value
                env["ir.config_parameter"].set_param("y.token", "1")  # secret-storage-ignore: secret-field: wrong rule
                env["ir.config_parameter"].set_param("z.token", "1")  # secret-storage-ignore: secret-config-parameter:
        """)
        self.assertEqual([f[1] for f in found], ["y.token", "z.token"])
        self.assertFalse(chk.silenced(["a"], 5, "secret-field"))


class FieldRuleTests(SecretStorageTestCase):
    def test_secret_field_on_a_model_fails(self):
        # Tests [@ANCHOR: secret_storage:field_findings]
        # Tests [@ANCHOR: secret_storage:is_odoo_model]
        found = self.findings("""
            class Host(models.Model):
                _name = "x.host"
                api_token = fields.Char()
                api_token_set = fields.Boolean()
                token_hint = fields.Char()
                secret_note = fields.Html()
                password = fields.Binary()

            class NotAModel:
                api_token = fields.Char()
        """)
        self.assertEqual([(f[0], f[1]) for f in found],
                         [("secret-field", "Host.api_token"), ("secret-field", "Host.password")])

    def test_test_files_and_unparsable_files_have_no_findings(self):
        # Tests [@ANCHOR: secret_storage:check_file]
        self.assertEqual(self.findings('set_param("a.secret", 1)\n', "mod/tests/test_a.py"), [])
        self.assertEqual(self.findings("def (:\n"), [])


class KnownListAndMainTests(SecretStorageTestCase):
    def test_known_list_and_report(self):
        # Tests [@ANCHOR: secret_storage:load_known]
        # Tests [@ANCHOR: secret_storage:report]
        # Tests [@ANCHOR: secret_storage:scan]
        # Tests [@ANCHOR: secret_storage:find_files]
        repo = os.path.join(self.tmp, "repo")
        _write(repo, "mod/hooks.py", 'def h(env):\n    env["ir.config_parameter"].set_param("mod.api_key", "k")\n')
        known = os.path.join(self.tmp, "known.json")
        entry = {"rule": "secret-config-parameter", "path": "mod/hooks.py", "symbol": "mod.api_key", "owner": "o",
                 "reason": "r"}
        stale = dict(entry, symbol="mod.gone_secret")
        with open(known, "w") as handle:
            json.dump({"entries": [entry, stale, {"rule": "nope", "path": "p", "symbol": "s", "owner": "o",
                                                  "reason": "r"}, {"rule": "secret-field"}]}, handle)
        entries, problems = chk.load_known(known)
        self.assertEqual(len(entries), 2)
        self.assertEqual(len(problems), 2)
        self.assertEqual(chk.load_known(os.path.join(self.tmp, "absent.json")), ([], []))
        self.assertIn(os.path.join(repo, "mod/hooks.py"), chk.find_files(repo))
        found = chk.scan([repo])
        lines, status = chk.report(found, entries[:1], [], [repo])
        self.assertEqual((lines, status), ([], 0))
        lines, status = chk.report(found, [], [], [repo])
        self.assertEqual(status, 1)
        self.assertTrue(lines[0].startswith("❌ secret-config-parameter mod/hooks.py:2:"))
        lines, status = chk.report(found, entries, [], [repo])
        self.assertEqual(status, 1)
        self.assertTrue(any("mod.gone_secret no longer has the finding" in line for line in lines))

    def test_main(self):
        # Tests [@ANCHOR: secret_storage:main]
        repo = os.path.join(self.tmp, "clean")
        _write(repo, "mod/models/a.py", "x = 1\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(chk.main([]), 1)
        with contextlib.redirect_stdout(out):
            # A clean tree passes even though the real known list names files it does not have (one root only).
            self.assertEqual(chk.main([repo]), 0)
        self.assertIn("secret storage: no new secret", out.getvalue())

    def test_the_real_known_list_is_well_formed(self):
        entries, problems = chk.load_known()
        self.assertEqual(problems, [])
        self.assertEqual(len({(e["rule"], e["path"], e["symbol"]) for e in entries}), len(entries))


if __name__ == "__main__":
    unittest.main()
