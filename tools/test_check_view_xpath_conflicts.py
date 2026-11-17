#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for check_view_xpath_conflicts.py (fixtures reproduce the round 138 pair)."""

import os
import subprocess
import sys
import tempfile
import unittest

import check_view_xpath_conflicts as chk

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_view_xpath_conflicts.py")
PARENT = "account_payment.portal_docs_entry"

CLICK = """<odoo><template id="{id}" inherit_id="{parent}">
<xpath expr="{expr}" position="attributes"><attribute name="onclick"/><attribute name="data-x">1</attribute></xpath>
</template></odoo>"""


def view(expr="//button[@onclick]", id="portal_docs_entry_no_inline_onclick", parent=PARENT, body=None):
    return (body or CLICK).format(id=id, parent=parent, expr=expr)


class ViewXpathConflictTests(unittest.TestCase):
    def scan(self, files):
        with tempfile.TemporaryDirectory() as root:
            for rel, text in files.items():
                path = os.path.join(root, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(text)
            return chk.check_view_xpath_conflicts([root]), root

    def modules(self, **views):
        files = {}
        for name, text in views.items():
            files[f"{name}/__manifest__.py"] = "{}\n"
            files[f"{name}/views/v.xml"] = text
        return files

    def test_round_138_pair_is_flagged_a_b_and_c(self):
        found, _ = self.scan(self.modules(ham_base=view(), content_security_policy=view()))
        kinds = {line[:3] for line in found}
        self.assertTrue({"(a)", "(b)", "(c)"} <= kinds, found)

    def test_single_view_is_clean(self):
        found, _ = self.scan(self.modules(ham_base=view()))
        self.assertEqual(found, [])

    def test_different_attributes_same_node_is_clean(self):
        other = view(expr="//button", id="other", body=CLICK.replace('<attribute name="onclick"/>', '<attribute name="class">x</attribute>'))
        mine = view(expr="//button", id="mine", body=CLICK.replace('<attribute name="onclick"/>', '<attribute name="title">y</attribute>').replace("data-x", "data-y"))
        found, _ = self.scan(self.modules(a=mine, b=other))
        self.assertEqual(found, [])

    def test_attribute_selector_removed_by_other_view_is_flagged_b(self):
        found, _ = self.scan(self.modules(a=view(expr="//button[@onclick]", id="a1"), b=view(expr="//div", id="b1", body=CLICK)))
        self.assertTrue(any(line.startswith("(b)") for line in found), found)

    def test_two_replaces_of_same_node_are_flagged_a(self):
        replace = '<odoo><template id="{id}" inherit_id="{parent}"><xpath expr="//p" position="replace"><p/></xpath></template></odoo>'
        found, _ = self.scan(self.modules(a=view(id="x", body=replace), b=view(id="y", body=replace)))
        self.assertTrue(any(line.startswith("(a)") for line in found), found)

    def test_two_inside_edits_are_clean(self):
        inside = '<odoo><template id="{id}" inherit_id="{parent}"><xpath expr="//div" position="inside"><p/></xpath></template></odoo>'
        found, _ = self.scan(self.modules(a=view(id="x", body=inside), b=view(id="x", body=inside)))
        self.assertEqual(found, [])

    def test_inactive_view_and_other_parent_are_ignored(self):
        inactive = CLICK.replace("<template ", '<template active="False" ')
        found, _ = self.scan(self.modules(a=view(), b=view(body=inactive)))
        self.assertEqual(found, [])
        found, _ = self.scan(self.modules(a=view(), b=view(parent="website.other")))
        self.assertEqual(found, [])

    def test_record_form_view_is_read(self):
        record = """<odoo><record id="{id}" model="ir.ui.view"><field name="inherit_id" ref="{parent}"/>
<field name="arch" type="xml"><xpath expr="//button[@onclick]" position="attributes"><attribute name="onclick"/></xpath></field></record></odoo>"""
        found, _ = self.scan(self.modules(a=view(), b=view(id="r", body=record)))
        self.assertTrue(any(line.startswith("(a)") for line in found), found)

    def test_command_line_exit_codes(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ("a", "b"):
                os.makedirs(os.path.join(root, name, "views"))
                open(os.path.join(root, name, "__manifest__.py"), "w").write("{}\n")
                open(os.path.join(root, name, "views", "v.xml"), "w").write(view())
            bad = subprocess.run([sys.executable, _SCRIPT, root], capture_output=True, text=True)
            self.assertEqual(bad.returncode, 1, bad.stdout)
            os.remove(os.path.join(root, "b", "views", "v.xml"))
            good = subprocess.run([sys.executable, _SCRIPT, root], capture_output=True, text=True)
            self.assertEqual(good.returncode, 0, good.stdout)


if __name__ == "__main__":
    unittest.main()
