#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_scss_math_functions.py.

The CASES table was probed against the real compiler Odoo uses (libsass, output_style=expanded,
precision=8) on 2026-10-12. When the python `sass` module is importable the tests re-run every
case through it, so a libsass upgrade that changes min()/max()/clamp() behaviour fails here
instead of silently invalidating the checker.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import check_scss_math_functions as chk  # noqa: E402

try:
    import sass
except ImportError:  # pragma: no cover - the dev box and the Odoo servers have libsass
    sass = None

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_scss_math_functions.py")

# (declaration value or whole rule, the compiler rejects it)
CASES = [
    # The incident.
    ("min-width: min(12rem, 100%)", True),
    ("min-width: unquote('min(12rem, 100%)')", False),
    ('min-width: unquote("min(12rem, 100%)")', False),
    # Mixed unit classes in min()/max().
    ("w: max(12rem, 100%)", True),
    ("w: min(10px, 1rem)", True),
    ("w: min(10px, 5vw)", True),
    ("w: min(100vw, 100vh)", True),
    ("w: max(1rem, 3vw)", True),
    ("w: min(1em, 1rem)", True),
    ("w: min(1ch, 1rem)", True),
    ("w: max(1fr, 1px)", True),
    ("w: min(1rem, 1rem*2, 5%)", True),
    ("w: min(1px, 1Q)", True),
    ("w: max(-1rem, 2px)", True),
    ("w: min(+1rem, 2%)", True),
    ("w: min(1.5REM, 100%)", True),
    ("w: min(12rem,\n 100%)", True),
    ("w: min(12rem, 100%) !important", True),
    ("w: min(12rem, 100% - 2rem)", True),
    # Same or convertible units, unitless numbers.
    ("w: min(1rem, 2rem)", False),
    ("w: max(1px, 2px)", False),
    ("w: min(50%, 20%)", False),
    ("w: min(10vw, 1vw)", False),
    ("w: min(1in, 10px)", False),
    ("w: min(1px, 1pt, 1cm)", False),
    ("w: min(1ms, 1s)", False),
    ("w: min(1deg, 1turn)", False),
    ("w: min(1rem, 2)", False),
    ("w: min(0, 100%)", False),
    ("w: min(12rem, 0)", False),
    ("w: min(1, 2)", False),
    # Non-numbers handed to the Sass built-in.
    ("w: max(1rem, var(--x))", True),
    ("w: min(var(--a), 100%)", True),
    ("w: min(env(safe-area-inset-top), 1rem)", True),
    ("w: min(calc(1rem + 2px), 100%)", True),
    ("w: min(clamp(1rem, 2vw, 3rem), 1rem)", True),
    ("w: min(1rem,\n#{2px})", True),
    # Evaluated everywhere outside the pass-through regions.
    ("w: var(--x, min(12rem, 100%))", True),
    ("w: translate(min(12rem, 100%))", True),
    ("w: foo(min(12rem,100%))", True),
    ("w: url(min(12rem,100%))", True),
    ("w: if(true, min(12rem,100%), 0)", True),
    ("w: min(12rem, 100%) min(1px, 2px)", True),
    ("--w: #{min(12rem, 100%)}", True),
    ("w: calc(1px + #{min(12rem, 100%)})", True),
    ("w: calc(100% - #{min(1rem, 2px)})", True),
    ("w: unquote('#{min(12rem, 100%)}')", True),
    ("w: #{min(12rem, 100%)}", True),
    ("w: clamp(1rem, min(5vw, 3rem), 2rem)", True),
    # Pass-through regions.
    ("w: calc(min(12rem, 100%))", False),
    ("w: calc(100% - max(1rem, 2%))", False),
    ("w: -webkit-calc(min(12rem, 100%))", False),
    ("w: -moz-calc(min(12rem, 100%))", False),
    ("--w: min(12rem, 100%)", False),
    ("content: 'min(12rem, 100%)'", False),
    ("w: unquote('min(' + 12rem + ', 100%)')", False),
    ("w: calc(#{unquote('min(12rem, 100%)')})", False),
    ("w: MIN(12rem, 100%)", False),
    ("w: Min(12rem, 100%)", False),
    # clamp() is not a Sass built-in, so its mixed units are fine; arithmetic in it is not.
    ("font-size: clamp(1.5rem, 2.6vw, 2.5rem)", False),
    ("w: clamp(1rem, 5%, 2rem)", False),
    ("w: clamp(10px, 1rem, 20px)", False),
    ("w: clamp(1rem, 2rem, 3px)", False),
    ("w: clamp(1px, 2rem, 3%)", False),
    ("w: clamp(1rem, var(--x), 2rem)", False),
    ("w: clamp(1rem, 2*3vw, 3rem)", False),
    ("w: clamp(1rem, 2vw * 2, 3rem)", False),
    ("w: clamp(1rem, 2vw - 1vw, 3rem)", False),
    ("w: clamp(1rem, 100%/3, 3rem)", False),
    ("w: clamp(1rem, 2vw -1rem, 3rem)", False),
    ("w: clamp(-1rem, -2vw, 3rem)", False),
    ("w: clamp(1rem, 100% - 2rem, 3rem)", True),
    ("w: clamp(1rem, 5vw + 1px, 2rem)", True),
    ("w: clamp(1rem, 5vw+1rem, 2rem)", True),
]


def _compiler_rejects(source):
    try:
        sass.compile(string=source, output_style="expanded", precision=8)
    except sass.CompileError:
        return True
    return False


def _rule(declaration):
    return "a { %s; }\n" % declaration


class ScssMathFunctionTests(unittest.TestCase):
    def test_every_case_is_judged_like_the_real_compiler(self):
        for declaration, expected in CASES:
            with self.subTest(declaration=declaration):
                self.assertEqual(bool(chk.check_scss_text(_rule(declaration))), expected)
                if sass is not None:
                    self.assertEqual(_compiler_rejects(_rule(declaration)), expected, "the table is stale")

    def test_comments_are_ignored(self):
        self.assertEqual(chk.check_scss_text("// min-width: min(12rem, 100%);\na { b: c; }\n"), [])
        self.assertEqual(chk.check_scss_text("/* min(12rem, 100%)\n   max(1px, 1rem) */ a { b: c; }\n"), [])

    def test_a_declaration_after_a_comment_is_still_checked(self):
        found = chk.check_scss_text("// fine\n/* fine */\na {\n    min-width: min(12rem, 100%);\n}\n", "x.scss")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("x.scss:4: "), found[0])

    def test_the_message_says_what_to_write(self):
        found = chk.check_scss_text(_rule("min-width: min(12rem, 100%)"), "lobbying.scss")
        self.assertIn('unquote("min(...)")', found[0])
        self.assertIn("Incompatible units", found[0])

    def test_dotted_and_prefixed_names_are_not_the_builtin(self):
        self.assertEqual(chk.check_scss_text(_rule("w: my-min(12rem, 100%)")), [])
        self.assertEqual(chk.check_scss_text(_rule("w: $min(12rem, 100%)")), [])

    def test_variables_are_skipped_not_guessed(self):
        self.assertEqual(chk.check_scss_text("$g: 1rem;\n" + _rule("w: min($g, 100%)")), [])

    def test_a_variable_does_not_hide_a_literal_conflict_elsewhere(self):
        self.assertEqual(len(chk.check_scss_text(_rule("w: min($g, 1rem, 100%)"))), 1)

    def test_unit_classes_follow_libsass(self):
        self.assertEqual(chk.unit_class("px"), chk.unit_class("in"))
        self.assertEqual(chk.unit_class("s"), chk.unit_class("ms"))
        self.assertNotEqual(chk.unit_class("px"), chk.unit_class("Q"))
        self.assertNotEqual(chk.unit_class("%"), chk.unit_class("rem"))
        self.assertEqual(chk.unit_class(""), "")


class ScssTreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rel, text):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_every_scss_file_in_the_tree_is_scanned_with_its_relative_path(self):
        self._write("mod_a/static/src/scss/ok.scss", _rule("w: clamp(1.5rem, 2.6vw, 2.5rem)"))
        self._write("mod_b/static/src/scss/bad.scss", _rule("w: min(12rem, 100%)"))
        self._write("mod_c/static/src/css/skip.css", _rule("w: min(12rem, 100%)"))
        self._write("mod_c/node_modules/x/y.scss", _rule("w: min(12rem, 100%)"))
        found = chk.check_scss_math_functions(self.tmp)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith(os.path.join("mod_b", "static", "src", "scss", "bad.scss") + ":1:"))

    def test_cli_scans_every_given_root_and_sets_the_exit_status(self):
        other = tempfile.mkdtemp()
        try:
            self._write("mod_a/static/src/scss/ok.scss", _rule("w: unquote('min(12rem, 100%)')"))
            res = subprocess.run([sys.executable, _SCRIPT, self.tmp, other], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stdout)
            with open(os.path.join(other, "bad.scss"), "w", encoding="utf-8") as handle:
                handle.write(_rule("w: max(1rem, 3vw)"))
            res = subprocess.run([sys.executable, _SCRIPT, self.tmp, other], capture_output=True, text=True)
            self.assertEqual(res.returncode, 1)
            self.assertIn("bad.scss:1", res.stdout)
        finally:
            shutil.rmtree(other, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
