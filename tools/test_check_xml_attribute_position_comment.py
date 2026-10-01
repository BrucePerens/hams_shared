#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_xml_attribute_position_comment.py.
"""

import os
import shutil
import tempfile
import unittest

import check_xml_attribute_position_comment as cxa  # noqa: E402


class CheckXmlAttributePositionCommentTests(unittest.TestCase):
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

    def test_flags_a_comment_between_two_attribute_elements(self):
        # The exact shape that caused this checker to be written: a comment interspersed
        # among several <attribute> siblings.
        self._write(
            "some_module/views/layout_overrides.xml",
            "<odoo>\n"
            '<template id="t" inherit_id="website.layout">\n'
            '<xpath expr="//html" position="attributes">\n'
            '<attribute name="t-att-data-foo">foo</attribute>\n'
            "<!-- an explanation that breaks everything -->\n"
            '<attribute name="t-att-data-bar">bar</attribute>\n'
            "</xpath>\n"
            "</template>\n"
            "</odoo>\n",
        )
        violations = cxa.check_xml_attribute_position_comment(self.tmp)
        self.assertEqual(len(violations), 1)
        self.assertIn("layout_overrides.xml", violations[0])

    def test_flags_a_comment_preceding_a_single_attribute_element(self):
        # The real, second instance this checker actually found live, 2026-10-01:
        # theme_hams/views/layout_overrides.xml's hams_dynamic_filter_template_blog_post_card
        # had an audit-ignore-xpath comment directly inside the <xpath> block, immediately
        # before its one <attribute> child -- not "between" two attributes, but still a
        # direct-child comment sibling, and still a real, confirmed silent failure.
        self._write(
            "theme_hams/views/layout_overrides.xml",
            "<odoo>\n"
            '<template id="t" inherit_id="website_blog.dynamic_filter_template_blog_post_card">\n'
            '<xpath expr="/*" position="attributes">\n'
            "<!-- audit-ignore-xpath: some note -->\n"
            '<attribute name="class" add="border-top-morse" separator=" "/>\n'
            "</xpath>\n"
            "</template>\n"
            "</odoo>\n",
        )
        violations = cxa.check_xml_attribute_position_comment(self.tmp)
        self.assertEqual(len(violations), 1)

    def test_does_not_flag_a_comment_before_the_xpath_element_itself(self):
        # The correct, fixed shape -- a comment as a sibling of <xpath>, not a child of it.
        self._write(
            "some_module/views/layout_overrides.xml",
            "<odoo>\n"
            '<template id="t" inherit_id="website.layout">\n'
            "<!-- an explanation, correctly placed before the xpath -->\n"
            '<xpath expr="//html" position="attributes">\n'
            '<attribute name="t-att-data-foo">foo</attribute>\n'
            '<attribute name="t-att-data-bar">bar</attribute>\n'
            "</xpath>\n"
            "</template>\n"
            "</odoo>\n",
        )
        violations = cxa.check_xml_attribute_position_comment(self.tmp)
        self.assertEqual(violations, [])

    def test_does_not_flag_a_comment_inside_an_unrelated_xpath_position(self):
        # Only position="attributes" has this trap -- a comment inside a position="inside"/
        # "after"/"before" xpath block is ordinary, unremarkable QWeb content.
        self._write(
            "some_module/views/layout_overrides.xml",
            "<odoo>\n"
            '<template id="t" inherit_id="website.layout">\n'
            '<xpath expr="//div" position="inside">\n'
            "<!-- a perfectly normal comment -->\n"
            "<span>hello</span>\n"
            "</xpath>\n"
            "</template>\n"
            "</odoo>\n",
        )
        violations = cxa.check_xml_attribute_position_comment(self.tmp)
        self.assertEqual(violations, [])

    def test_does_not_flag_a_comment_inside_an_attribute_elements_own_text(self):
        # A comment INSIDE one <attribute> element's own text content (not as a sibling of
        # it) is a different, unrelated shape this checker correctly leaves alone.
        self._write(
            "some_module/views/layout_overrides.xml",
            "<odoo>\n"
            '<template id="t" inherit_id="website.layout">\n'
            '<xpath expr="//div" position="attributes">\n'
            '<attribute name="class">plain-value</attribute>\n'
            "</xpath>\n"
            "</template>\n"
            "</odoo>\n",
        )
        violations = cxa.check_xml_attribute_position_comment(self.tmp)
        self.assertEqual(violations, [])

    def test_a_malformed_xml_file_is_skipped_not_crashed_on(self):
        self._write("some_module/views/broken.xml", "<odoo><unclosed>\n")
        violations = cxa.check_xml_attribute_position_comment(self.tmp)
        self.assertEqual(violations, [])

    def test_non_xml_files_are_ignored(self):
        self._write("some_module/views/notes.txt", "<xpath position=\"attributes\"><!-- x --></xpath>")
        violations = cxa.check_xml_attribute_position_comment(self.tmp)
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
