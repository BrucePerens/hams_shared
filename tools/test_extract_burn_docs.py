#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for extract_burn_docs.py.

Its `if __name__ == "__main__":` block hardcodes both source_file and
output_file relative to its own __file__ (check_burn_list.py alongside
it, linter_rules.md written next to it), with no CLI args -- so, like
scan_ui_vulnerabilities.py's main(), it can't be pointed at a fixture
directly. MainScriptTests copies the script into a fixture directory
alongside a fixture check_burn_list.py and runs it from there, so the
real script writes its real output file into the fixture, never the
real repo tree.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import extract_burn_docs as chk  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extract_burn_docs.py")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class ExtractDocsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _path(self, content):
        p = os.path.join(self.tmp, "source.py")
        _write(p, content)
        return p

    def test_a_bang_prefixed_docstring_expression_is_extracted_with_the_bang_and_leading_space_stripped(self):
        p = self._path('"!Do not do the bad thing."\n')
        self.assertEqual(chk.extract_docs(p), ["Do not do the bad thing."])

    def test_multiple_bang_docs_are_extracted_in_source_order(self):
        p = self._path('"!First rule."\nx = 1\n"!Second rule."\n')
        self.assertEqual(chk.extract_docs(p), ["First rule.", "Second rule."])

    def test_a_plain_string_expression_without_a_leading_bang_is_ignored(self):
        p = self._path('"just a comment string, not a doc"\n')
        self.assertEqual(chk.extract_docs(p), [])

    def test_a_regular_module_docstring_without_a_bang_is_ignored(self):
        p = self._path('"""A normal module docstring."""\nx = 1\n')
        self.assertEqual(chk.extract_docs(p), [])

    def test_a_string_that_is_not_a_standalone_expression_statement_is_ignored(self):
        # e.g. assigned to a variable, or passed as a function argument --
        # only bare ast.Expr(ast.Constant(str)) statements count.
        p = self._path('x = "!looks like a doc but is assigned"\n')
        self.assertEqual(chk.extract_docs(p), [])

    def test_a_file_with_no_matching_strings_returns_an_empty_list(self):
        p = self._path("x = 1\ny = 2\n")
        self.assertEqual(chk.extract_docs(p), [])

    def test_surrounding_whitespace_after_the_bang_is_stripped(self):
        p = self._path('"!   spaced out rule.   "\n')
        self.assertEqual(chk.extract_docs(p), ["spaced out rule."])

    def test_an_fstring_docstring_is_not_a_plain_constant_and_is_ignored(self):
        # ast.JoinedStr (f-strings) is not ast.Constant, so this checker's
        # narrow isinstance check deliberately can't see into one.
        p = self._path('x = "y"\nf"!looks like a rule {x}"\n')
        self.assertEqual(chk.extract_docs(p), [])


class MainScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        shutil.copy(_SCRIPT, os.path.join(self.tmp, "extract_burn_docs.py"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        result = subprocess.run(
            [sys.executable, os.path.join(self.tmp, "extract_burn_docs.py")],
            capture_output=True,
            text=True,
            cwd=self.tmp,
            timeout=30,
        )
        return result.returncode, result.stdout + result.stderr

    def test_extracted_docs_are_written_as_a_markdown_bullet_list_next_to_the_script(self):
        _write(
            os.path.join(self.tmp, "check_burn_list.py"),
            '"!First rule text."\nx = 1\n"!Second rule text."\n"not a doc string"\n',
        )
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertIn("Extracted 2 literate documentation strings", out)
        output_path = os.path.join(self.tmp, "linter_rules.md")
        self.assertTrue(os.path.exists(output_path))
        with open(output_path, encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(
            content,
            "# Linter Rules (Burn List)\n\n- First rule text.\n- Second rule text.\n",
        )

    def test_no_matching_docs_writes_a_header_only_file(self):
        _write(os.path.join(self.tmp, "check_burn_list.py"), "x = 1\n")
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertIn("Extracted 0 literate documentation strings", out)
        with open(os.path.join(self.tmp, "linter_rules.md"), encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, "# Linter Rules (Burn List)\n\n")


if __name__ == "__main__":
    unittest.main()
