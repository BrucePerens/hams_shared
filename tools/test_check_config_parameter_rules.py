# SPDX-License-Identifier: AGPL-3.0-or-later
"""check_config_parameter_rules: a service group's ir.config_parameter row needs a key rule in the same module."""
import os
import tempfile
import unittest

import check_config_parameter_rules as check

HEADER = "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
RULE = """<odoo><data noupdate="1"><record id="r" model="ir.rule">
<field name="model_id" ref="base.model_ir_config_parameter"/>
<field name="groups" eval="[(4, ref('%s'))]"/>
<field name="domain_force">[('key', '=like', 'm.%%')]</field></record></data></odoo>"""


class CheckCsvTests(unittest.TestCase):
    def _module(self, rows, rule_group=None):
        root = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, root)
        os.makedirs(os.path.join(root, "m", "security"))
        path = os.path.join(root, "m", "security", "ir.model.access.csv")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(HEADER + rows)
        if rule_group:
            with open(os.path.join(root, "m", "security", "rules.xml"), "w", encoding="utf-8") as handle:
                handle.write(RULE % rule_group)
        return path

    def test_a_blanket_row_without_a_rule_is_reported(self):
        path = self._module("a,a,base.model_ir_config_parameter,group_svc,1,0,0,0\n")
        self.assertEqual(len(check._check_csv("m", path)), 1)

    def test_a_row_with_a_rule_for_its_group_is_clean(self):
        path = self._module("a,a,base.model_ir_config_parameter,group_svc,1,0,0,0\n", rule_group="m.group_svc")
        self.assertEqual(check._check_csv("m", path), [])

    def test_a_rule_for_another_group_does_not_cover_the_row(self):
        path = self._module("a,a,base.model_ir_config_parameter,group_svc,1,0,0,0\n", rule_group="m.group_other")
        self.assertEqual(len(check._check_csv("m", path)), 1)

    def test_the_config_service_gatekeeper_is_exempt(self):
        path = self._module("a,a,base.model_ir_config_parameter,ham_base.group_config_service,1,1,1,1\n")
        self.assertEqual(check._check_csv("m", path), [])

    def test_other_models_are_ignored(self):
        path = self._module("a,a,base.model_res_partner,group_svc,1,0,0,0\n")
        self.assertEqual(check._check_csv("m", path), [])


if __name__ == "__main__":
    unittest.main()
