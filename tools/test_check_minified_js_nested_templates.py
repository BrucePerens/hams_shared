#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_minified_js_nested_templates.py.

find_nested_template_literals() is a hand-rolled character scanner (not a
real JS parser, by its own docstring's admission) detecting the exact
defect class that caused a real production bug (hams_com f1f00511:
ham_events/static/src/lib/transformers.min.js corrupted by rjsmin). Given
that history, this scanner gets the most thorough coverage in the sweep --
including the scanner's own documented false-negative case (a regex literal
containing a brace), verified as real current behavior, not assumed.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_minified_js_nested_templates as chk


class FindNestedTemplateLiteralsTests(unittest.TestCase):
    def test_no_template_literals_at_all(self):
        self.assertEqual(chk.find_nested_template_literals("var x = 1;"), [])

    def test_an_ordinary_unnested_template_literal_is_not_flagged(self):
        self.assertEqual(
            chk.find_nested_template_literals("var x = `hello ${name}`;"), []
        )

    def test_multiple_ordinary_template_literals_are_not_flagged(self):
        code = "var a = `x ${1}`; var b = `y ${2}`;"
        self.assertEqual(chk.find_nested_template_literals(code), [])

    def test_a_genuinely_nested_template_literal_is_flagged(self):
        code = "var x = `outer ${cond ? `inner` : 'x'}`;"
        findings = chk.find_nested_template_literals(code)
        self.assertEqual(len(findings), 1)
        line, col = findings[0]
        self.assertEqual(line, 1)
        # Column of the inner backtick that opens `inner`.
        self.assertEqual(code[col - 1], "`")

    def test_both_ternary_branches_nested_are_both_flagged(self):
        code = "var x = `outer ${cond ? `a` : `b`}`;"
        findings = chk.find_nested_template_literals(code)
        self.assertEqual(len(findings), 2)

    def test_a_backtick_inside_a_quoted_string_within_a_substitution_is_not_flagged(self):
        # The single-quoted string '`' contains a literal backtick
        # character, but it's inside an ordinary string, not a real nested
        # template literal -- the scanner must skip over quoted strings
        # entirely while inside a ${...} substitution.
        code = "var x = `a ${'`'} b`;"
        self.assertEqual(chk.find_nested_template_literals(code), [])

    def test_an_object_literal_inside_a_substitution_does_not_desync_brace_depth(self):
        code = "var x = `a ${ {b: 1} } c`;"
        self.assertEqual(chk.find_nested_template_literals(code), [])

    def test_a_line_comment_inside_a_substitution_hiding_a_backtick_is_ignored(self):
        code = "var x = `a ${ // comment with ` backtick\n b} c`;"
        self.assertEqual(chk.find_nested_template_literals(code), [])

    def test_a_block_comment_inside_a_substitution_hiding_a_backtick_is_ignored(self):
        code = "var x = `a ${ /* ` */ b} c`;"
        self.assertEqual(chk.find_nested_template_literals(code), [])

    def test_an_escaped_backtick_in_plain_text_does_not_close_the_literal_early(self):
        code = r"var x = `a \` b ${y}`;"
        self.assertEqual(chk.find_nested_template_literals(code), [])

    def test_documents_the_scanners_own_known_false_negative_for_a_brace_in_a_regex(self):
        # find_nested_template_literals()'s own docstring documents this as
        # an accepted tradeoff: it doesn't disambiguate regex literals from
        # division, so a brace inside a regex like /{2,3}/ can desync the
        # depth count. Documenting the real, current behavior here (not
        # claiming it's correct) so a future change to this tradeoff is a
        # deliberate, visible diff instead of a silent behavior change.
        code = "var x = `a ${/{2,3}/} b`;"
        self.assertEqual(chk.find_nested_template_literals(code), [])

    def test_line_and_column_numbers_are_correct_across_multiple_lines(self):
        code = "var x = `line1\n${\n  cond ? `bad` : 1\n}`;"
        findings = chk.find_nested_template_literals(code)
        self.assertEqual(len(findings), 1)
        line, col = findings[0]
        self.assertEqual(line, 3)
        lines = code.split("\n")
        self.assertEqual(lines[line - 1][col - 1], "`")

    def test_a_nested_backtick_that_itself_contains_a_substitution_is_still_tracked_correctly(self):
        # After flagging the inner backtick, the scanner pushes a new TEXT
        # frame for it so subsequent characters parse coherently (matching
        # rjsmin's own actual "closes the outer literal at the first
        # backtick" failure mode) -- a further ${...} inside that inner
        # literal must not throw the scanner off or produce a spurious
        # extra finding.
        code = "var x = `outer ${cond ? `inner ${y}` : 'x'}`;"
        findings = chk.find_nested_template_literals(code)
        self.assertEqual(len(findings), 1)


class CollectMinifiedJsAssetsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, path, content):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def test_collects_js_assets_from_a_real_manifest_on_disk(self):
        self._write(
            os.path.join(self.tmp, "mod_a", "__manifest__.py"),
            "{\n"
            "    'assets': {\n"
            "        'web.assets_backend': ['mod_a/static/src/js/a.js', 'mod_a/static/src/js/b.js'],\n"
            "    },\n"
            "}\n",
        )
        result = chk.collect_minified_js_assets(self.tmp)
        self.assertEqual(
            set(result.keys()), {"mod_a/static/src/js/a.js", "mod_a/static/src/js/b.js"}
        )

    def test_non_js_assets_are_not_collected(self):
        self._write(
            os.path.join(self.tmp, "mod_a", "__manifest__.py"),
            "{\n"
            "    'assets': {\n"
            "        'web.assets_backend': ['mod_a/static/src/css/a.css', 'mod_a/static/src/js/b.js'],\n"
            "    },\n"
            "}\n",
        )
        result = chk.collect_minified_js_assets(self.tmp)
        self.assertEqual(set(result.keys()), {"mod_a/static/src/js/b.js"})

    def test_a_js_asset_bundled_under_two_keys_records_both_bundle_names(self):
        self._write(
            os.path.join(self.tmp, "mod_a", "__manifest__.py"),
            "{\n"
            "    'assets': {\n"
            "        'web.assets_backend': ['mod_a/static/src/js/a.js'],\n"
            "        'web.assets_frontend': ['mod_a/static/src/js/a.js'],\n"
            "    },\n"
            "}\n",
        )
        result = chk.collect_minified_js_assets(self.tmp)
        self.assertEqual(
            set(result["mod_a/static/src/js/a.js"]), {"web.assets_backend", "web.assets_frontend"}
        )

    def test_a_module_with_no_assets_key_contributes_nothing(self):
        self._write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        self.assertEqual(chk.collect_minified_js_assets(self.tmp), {})

    def test_a_syntax_broken_manifest_is_reported_and_skipped_without_crashing(self):
        self._write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{ broken")
        result = chk.collect_minified_js_assets(self.tmp)
        self.assertEqual(result, {})


class ResolveAssetPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_resolves_against_the_first_matching_search_root(self):
        real = os.path.join(self.tmp, "root_a", "mod_a", "static", "src", "js", "a.js")
        os.makedirs(os.path.dirname(real))
        with open(real, "w") as f:
            f.write("// x")
        result = chk.resolve_asset_path(
            "mod_a/static/src/js/a.js",
            [os.path.join(self.tmp, "root_a"), os.path.join(self.tmp, "root_b")],
        )
        self.assertEqual(result, real)

    def test_returns_none_when_no_search_root_has_the_file(self):
        result = chk.resolve_asset_path(
            "mod_a/static/src/js/missing.js", [os.path.join(self.tmp, "root_a")]
        )
        self.assertIsNone(result)


class MainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, path, content):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def _run(self):
        import subprocess
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_minified_js_nested_templates.py")
        result = subprocess.run(
            [sys.executable, script, self.tmp], capture_output=True, text=True, timeout=30
        )
        return result.returncode, result.stdout + result.stderr

    def test_a_clean_bundled_js_file_passes(self):
        self._write(
            os.path.join(self.tmp, "mod_a", "__manifest__.py"),
            "{\n"
            "    'assets': {'web.assets_backend': ['mod_a/static/src/js/a.js']},\n"
            "}\n",
        )
        self._write(
            os.path.join(self.tmp, "mod_a", "static", "src", "js", "a.js"),
            "var x = `hello ${name}`;\n",
        )
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertIn("all clean", out)

    def test_a_bundled_js_file_with_a_nested_template_literal_fails(self):
        self._write(
            os.path.join(self.tmp, "mod_a", "__manifest__.py"),
            "{\n"
            "    'assets': {'web.assets_backend': ['mod_a/static/src/js/a.js']},\n"
            "}\n",
        )
        self._write(
            os.path.join(self.tmp, "mod_a", "static", "src", "js", "a.js"),
            "var x = `outer ${cond ? `inner` : 'x'}`;\n",
        )
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("NESTED TEMPLATE LITERAL", out)
        self.assertIn("web.assets_backend", out)

    def test_no_bundled_js_assets_at_all_passes_with_its_own_message(self):
        self._write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertIn("No bundled JS assets found", out)

    def test_an_asset_declared_but_not_present_on_disk_is_silently_skipped(self):
        # Referenced from another installed module outside this repo's own
        # tree -- not this checker's file to read, by its own comment.
        self._write(
            os.path.join(self.tmp, "mod_a", "__manifest__.py"),
            "{\n"
            "    'assets': {'web.assets_backend': ['mod_a/static/src/js/does_not_exist.js']},\n"
            "}\n",
        )
        code, out = self._run()
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
