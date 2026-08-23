#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for generate_pot.py.

find_translatable_strings() and generate_pot() are both pure, fixture-
driven functions with no dangerous module-level side effects (only the
__main__ block writes to a real repo path, and it only runs under
`python3 generate_pot.py`, never on import), so these tests exercise
them directly against real temp-directory fixtures.
"""

import os
import shutil
import sys
import tempfile
import unittest
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import generate_pot as gp  # noqa: E402


def _write(root, rel, content):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return p


class FindTranslatableStringsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_bare_underscore_call_with_a_static_string_is_extracted(self):
        _write(self.tmp, "mod_a/models.py", 'x = _("Hello world")\n')
        result = gp.find_translatable_strings(self.tmp)
        self.assertIn("Hello world", result)
        self.assertEqual(result["Hello world"], [("mod_a/models.py", 1)])

    def test_an_attribute_call_named_underscore_is_not_treated_as_gettext(self):
        # node.func is an ast.Attribute here, not ast.Name, so it has no
        # .id -- the AttributeError is caught and func_id stays None.
        _write(self.tmp, "mod_a/other.py", 'y = obj._("not gettext")\n')
        result = gp.find_translatable_strings(self.tmp)
        self.assertNotIn("not gettext", result)

    def test_a_non_static_argument_to_underscore_is_not_extracted(self):
        _write(self.tmp, "mod_a/dyn.py", 'name = "x"\nz = _(name)\n')
        result = gp.find_translatable_strings(self.tmp)
        self.assertEqual(dict(result), {})

    def test_a_call_to_underscore_with_no_arguments_is_not_extracted(self):
        _write(self.tmp, "mod_a/empty.py", "z = _()\n")
        result = gp.find_translatable_strings(self.tmp)
        self.assertEqual(dict(result), {})

    def test_a_file_with_a_syntax_error_is_skipped_with_a_warning_not_a_crash(self):
        _write(self.tmp, "mod_a/bad.py", "def f(:\n")
        # No assertion on stdout content here (the script uses plain
        # print(), not logging, for the syntax-error branch) -- just that
        # the walk completes and returns without raising.
        result = gp.find_translatable_strings(self.tmp)
        self.assertEqual(dict(result), {})

    def test_the_script_never_scans_its_own_file_by_name(self):
        _write(self.tmp, "mod_a/generate_pot.py", 'w = _("should be excluded")\n')
        result = gp.find_translatable_strings(self.tmp)
        self.assertNotIn("should be excluded", result)

    def test_a_hidden_directory_is_pruned(self):
        _write(self.tmp, ".hidden/skip.py", 'a = _("hidden dir excluded")\n')
        result = gp.find_translatable_strings(self.tmp)
        self.assertNotIn("hidden dir excluded", result)

    def test_pycache_and_node_modules_are_pruned(self):
        _write(self.tmp, "__pycache__/skip.py", 'a = _("pycache excluded")\n')
        _write(self.tmp, "node_modules/skip.py", 'a = _("node_modules excluded")\n')
        result = gp.find_translatable_strings(self.tmp)
        self.assertNotIn("pycache excluded", result)
        self.assertNotIn("node_modules excluded", result)

    def test_the_same_msgid_across_two_files_accumulates_both_occurrences(self):
        _write(self.tmp, "mod_a/models.py", 'x = _("Hello world")\n')
        _write(self.tmp, "mod_b/dup.py", 'x = _("Hello world")\n')
        result = gp.find_translatable_strings(self.tmp)
        self.assertEqual(
            sorted(result["Hello world"]),
            sorted([("mod_a/models.py", 1), ("mod_b/dup.py", 1)]),
        )

    def test_a_non_python_file_is_never_scanned(self):
        _write(self.tmp, "mod_a/notes.txt", '_("should not be scanned")\n')
        result = gp.find_translatable_strings(self.tmp)
        self.assertEqual(dict(result), {})


class GeneratePotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_missing_parent_directories(self):
        out = os.path.join(self.tmp, "i18n", "nested", "hams_master.pot")
        gp.generate_pot(defaultdict(list), out)
        self.assertTrue(os.path.exists(out))

    def test_writes_the_standard_gettext_header(self):
        out = os.path.join(self.tmp, "out.pot")
        gp.generate_pot(defaultdict(list), out)
        with open(out, encoding="utf-8") as f:
            content = f.read()
        self.assertIn('msgid ""', content)
        self.assertIn('"Content-Type: text/plain; charset=UTF-8\\n"', content)
        self.assertIn('"Project-Id-Version: Odoo Server 19.0\\n"', content)

    def test_entries_are_sorted_alphabetically_by_msgid(self):
        translations = defaultdict(list)
        translations["Zebra"] = [("a.py", 1)]
        translations["Apple"] = [("b.py", 1)]
        out = os.path.join(self.tmp, "out.pot")
        gp.generate_pot(translations, out)
        with open(out, encoding="utf-8") as f:
            content = f.read()
        self.assertLess(content.index('msgid "Apple"'), content.index('msgid "Zebra"'))

    def test_occurrences_for_one_msgid_are_sorted_by_file_then_line(self):
        translations = defaultdict(list)
        translations["Hello"] = [("z.py", 9), ("a.py", 5), ("a.py", 1)]
        out = os.path.join(self.tmp, "out.pot")
        gp.generate_pot(translations, out)
        with open(out, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("#: a.py:1\n#: a.py:5\n#: z.py:9\nmsgid \"Hello\"", content)

    def test_backslashes_quotes_and_newlines_in_a_msgid_are_escaped(self):
        translations = defaultdict(list)
        translations['Say "hi"\\bye\nline2'] = [("a.py", 1)]
        out = os.path.join(self.tmp, "out.pot")
        gp.generate_pot(translations, out)
        with open(out, encoding="utf-8") as f:
            content = f.read()
        self.assertIn('msgid "Say \\"hi\\"\\\\bye\\nline2"', content)

    def test_every_msgid_gets_an_empty_msgstr(self):
        translations = defaultdict(list)
        translations["Hello"] = [("a.py", 1)]
        out = os.path.join(self.tmp, "out.pot")
        gp.generate_pot(translations, out)
        with open(out, encoding="utf-8") as f:
            content = f.read()
        self.assertIn('msgid "Hello"\nmsgstr ""\n\n', content)


if __name__ == "__main__":
    unittest.main()
