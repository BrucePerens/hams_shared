#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_service_account_acl.py.
"""

import os
import shutil
import tempfile
import unittest

import check_service_account_acl as csa  # noqa: E402

USER_XML = """<odoo><data>
<record id="group_svc" model="res.groups"/>
<record id="group_base" model="res.groups"/>
<record id="svc_user" model="res.users">
  <field name="group_ids" eval="[(6, 0, [ref('mod.group_svc')])]"/>
</record>
</data></odoo>"""


class CheckServiceAccountAclTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._write("mod/__manifest__.py", "{'name': 'mod', 'depends': ['base']}\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, relpath, content):
        path = os.path.join(self.tmp, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)

    def _setup(self, csv_rows, code, xml=USER_XML):
        self._write("mod/security/data.xml", xml)
        head = "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        self._write("mod/security/ir.model.access.csv", head + csv_rows)
        with_uid = (
            "class Thing(models.Model):\n"
            "    _name = 'thing.a'\n"
            "    def go(self):\n"
            "        svc_uid = self.env['zero_sudo.security.utils']._get_service_uid('mod.svc_user')\n"
        )
        self._write("mod/models/thing.py", with_uid + code)
        return csa.check_service_account_acl([self.tmp], core_addons="")

    def test_flags_create_without_acl(self):
        found = self._setup(
            "r,r,model_thing_a,mod.group_svc,1,0,0,0\n",
            "        self.env['thing.a'].with_user(svc_uid).create({})\n",
        )
        self.assertEqual(len(found), 1)
        self.assertIn("thing.py:5 mod.svc_user has no create ACL on thing.a", found[0])

    def test_passes_when_the_group_has_the_row(self):
        found = self._setup(
            "r,r,model_thing_a,mod.group_svc,1,1,1,1\n",
            "        self.env['thing.a'].with_user(svc_uid).create({})\n",
        )
        self.assertEqual(found, [])

    def test_a_row_for_another_group_does_not_count(self):
        found = self._setup(
            "r,r,model_thing_a,mod.group_base,1,1,1,1\n",
            "        self.env['thing.a'].with_user(svc_uid).write({})\n",
        )
        self.assertEqual(len(found), 1)

    def test_a_row_with_no_group_counts_for_everybody(self):
        found = self._setup(
            "r,r,model_thing_a,,1,1,1,1\n",
            "        self.env['thing.a'].with_user(svc_uid).write({})\n",
        )
        self.assertEqual(found, [])

    def test_implied_group_counts(self):
        xml = USER_XML.replace(
            '<record id="group_base" model="res.groups"/>',
            '<record id="group_base" model="res.groups"/>'
            '<record id="group_svc" model="res.groups"><field name="implied_ids" eval="[(4, ref(\'group_base\'))]"/></record>',
        )
        found = self._setup(
            "r,r,model_thing_a,mod.group_base,1,1,1,1\n",
            "        self.env['thing.a'].with_user(svc_uid).unlink()\n",
            xml,
        )
        self.assertEqual(found, [])

    def test_function_write_of_implied_ids_counts(self):
        xml = USER_XML.replace(
            "</data>",
            '<function model="res.groups" name="write"><value eval="[ref(\'mod.group_svc\')]"/>'
            "<value eval=\"{'implied_ids': [(4, ref('mod.group_base'))]}\"/></function></data>",
        )
        found = self._setup(
            "r,r,model_thing_a,mod.group_base,1,1,1,1\n",
            "        self.env['thing.a'].with_user(svc_uid).create({})\n",
            xml,
        )
        self.assertEqual(found, [])

    def test_follows_a_search_result_into_a_variable(self):
        found = self._setup(
            "r,r,model_thing_a,mod.group_svc,1,1,0,0\n",
            "        rows = self.env['thing.a'].with_user(svc_uid).search([])\n"
            "        rows.unlink()\n",
        )
        self.assertEqual(len(found), 1)
        self.assertIn("no unlink ACL on thing.a", found[0])

    def test_service_env_subscript(self):
        found = self._setup(
            "r,r,model_thing_a,mod.group_svc,1,0,0,0\n",
            "        env_svc = self.env['zero_sudo.security.utils']._get_service_env('mod.svc_user')\n"
            "        env_svc['thing.a'].create({})\n",
        )
        self.assertEqual(len(found), 1)

    def test_self_is_the_class_model(self):
        found = self._setup(
            "r,r,model_thing_a,mod.group_svc,1,0,0,0\n",
            "        self.with_user(svc_uid).write({})\n",
        )
        self.assertEqual(len(found), 1)

    def test_message_post_needs_mail_message_create(self):
        found = self._setup(
            "r,r,model_thing_a,mod.group_svc,1,0,0,0\n",
            "        self.with_user(svc_uid).message_post(body='x')\n",
        )
        self.assertEqual(len(found), 1)
        self.assertIn("no create ACL on mail.message", found[0])

    def test_helper_method_returning_the_service_uid(self):
        found = self._setup(
            "r,r,model_thing_a,mod.group_svc,1,0,0,0\n",
            "        self.env['thing.a'].with_user(self._svc()).create({})\n"
            "    def _svc(self):\n"
            "        return self.env['zero_sudo.security.utils']._get_service_uid('mod.svc_user')\n",
        )
        self.assertEqual(len(found), 1)

    def test_module_constant_as_the_xml_id(self):
        self._write("mod/security/data.xml", USER_XML)
        self._write("mod/security/ir.model.access.csv", "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n")
        self._write(
            "mod/models/thing.py",
            "SVC = 'mod.svc_user'\n"
            "class Thing(models.Model):\n"
            "    _name = 'thing.a'\n"
            "    def go(self):\n"
            "        uid = self.env['zero_sudo.security.utils']._get_service_uid(SVC)\n"
            "        self.env['thing.a'].with_user(uid).create({})\n",
        )
        self.assertEqual(len(csa.check_service_account_acl([self.tmp], core_addons="")), 1)

    def test_ignore_marker_on_the_call(self):
        found = self._setup(
            "r,r,model_thing_a,mod.group_svc,1,0,0,0\n",
            "        self.env['thing.a'].with_user(svc_uid).create({})  # audit-ignore-service-acl: row added by a hook\n",
        )
        self.assertEqual(found, [])

    def test_non_service_user_and_config_parameter_are_not_checked(self):
        found = self._setup(
            "r,r,model_thing_a,mod.group_svc,1,0,0,0\n",
            "        self.env['thing.a'].with_user(self.env.user).create({})\n"
            "        self.env['ir.config_parameter'].with_user(svc_uid).set_param('ham.x', '1')\n",
        )
        self.assertEqual(found, [])

    def test_abstract_mixin_self_is_not_a_model(self):
        self._write("mod/security/data.xml", USER_XML)
        self._write("mod/security/ir.model.access.csv", "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n")
        self._write(
            "mod/models/thing.py",
            "class Mixin(models.AbstractModel):\n"
            "    _name = 'thing.mixin'\n"
            "    def go(self):\n"
            "        uid = self.env['zero_sudo.security.utils']._get_service_uid('mod.svc_user')\n"
            "        self.with_user(uid).write({})\n",
        )
        self.assertEqual(csa.check_service_account_acl([self.tmp], core_addons=""), [])


if __name__ == "__main__":
    unittest.main()


# Tests [@ANCHOR: check_service_account_acl:acl_class]
# Tests [@ANCHOR: check_service_account_acl:filescan_class]
# Both classes are exercised through csa.check_service_account_acl in the cases above.
