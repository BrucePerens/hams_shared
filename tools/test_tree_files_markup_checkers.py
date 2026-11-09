# SPDX-License-Identifier: AGPL-3.0-or-later

"""The markup, stylesheet and JavaScript checkers that select their files with `tree_files` (XML, SCSS, Owl templates,
documentation images, minified-asset manifests, JS syntax, Hoot coverage) never scan a worktree of another checkout nested
under the one being linted, and still scan the files that are the tree's own, new unadded ones included. See
`tree_files_testkit.check_nested_worktree_table`."""

import os
import shutil
import tempfile
import unittest

import check_owl_templates
import tree_files_testkit as kit

CHECKERS = {
    "check_doc_images.py": ({"m/README.md": "text\n"}, "m/README.md"),
    "check_minified_js_nested_templates.py": ({"m/__manifest__.py": "{'assets': {}}\n"}, "m/__manifest__.py"),
    "check_minified_js_syntax_errors.py": ({"m/__manifest__.py": "{'assets': {}}\n"}, "m/__manifest__.py"),
    "check_scss_math_functions.py": ({"m/a.scss": "a { b: c; }\n"}, "m/a.scss"),
    "check_xml_comment_double_hyphen.py": ({"m/a.xml": "<odoo/>\n"}, "m/a.xml"),
    "check_xml_attribute_position_comment.py": ({"m/a.xml": "<odoo/>\n"}, "m/a.xml"),
    "check_js_syntax.py": ({"m/static/src/a.js": "var a = 1;\n"}, "m/static/src/a.js"),
    "check_hoot_runner_coverage.py": (
        {"m/__manifest__.py": "{}\n", "m/tests/test_a.py": "x = 1\n", "m/static/tests/a.test.js": "x\n"},
        "m/tests/test_a.py",
    ),
}


class NestedForeignWorktreeTests(unittest.TestCase):
    def test_a_nested_foreign_worktree_is_not_scanned_and_a_new_file_is(self):
        kit.check_nested_worktree_table(self, CHECKERS)

    def test_owl_template_files_come_from_this_tree_only(self):
        tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        kit.checkout_files_with_nested_worktree(
            tmp, {"m/static/src/xml/a.xml": "<templates/>\n", "m/static/src/other/b.xml": "<odoo/>\n"}
        )
        kit.write(tmp, "m/static/src/xml/new.xml", "<templates/>\n")
        got = [os.path.relpath(p, tmp) for p in check_owl_templates.find_owl_template_files(tmp)]
        self.assertEqual(sorted(got), [os.path.join("m", "static", "src", "xml", n) for n in ("a.xml", "new.xml")])


if __name__ == "__main__":
    unittest.main()
