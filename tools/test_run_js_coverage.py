#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for run_js_coverage.py's bundle-offset parser.

Fixtures use the exact real header format confirmed live against Odoo's own debug-mode asset
bundle output (ANCHOR_COVERAGE_AND_REMEDIATION_PLAN.md's JS Stage 2 section, 2026-09-07) --
`ham_dns`'s real DNS tour test run under `debug=assets`, not a guessed-at format.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run_js_coverage import parse_bundle_module_offsets, resolve_addon_static_path  # noqa: E402


def _module_header(filepath, lines):
    stars = "*" * max(len(f"  Filepath: {filepath}  "), len(f"  Lines: {lines}  "))
    return (
        f"/{stars}\n"
        f"*  Filepath: {filepath}  *\n"
        f"*  Lines: {lines}                               *\n"
        f"{stars}/\n"
    )


class ParseBundleModuleOffsetsTests(unittest.TestCase):
    def test_a_single_real_shaped_module_is_parsed_with_correct_line_range(self):
        # The exact real sample confirmed live: a header, then a body of a known real line count.
        header = (
            "/*********************************************\n"
            "*  Filepath: /web/static/lib/luxon/luxon.js  *\n"
            "*  Lines: 3                               *\n"
            "*********************************************/\n"
        )
        body = "line one\nline two\nline three\n"
        bundle = header + body
        modules = parse_bundle_module_offsets(bundle)
        self.assertEqual(len(modules), 1)
        start, end, filepath = modules[0]
        self.assertEqual(filepath, "/web/static/lib/luxon/luxon.js")
        self.assertEqual(start, 5)  # header is 4 lines, body starts on line 5
        self.assertEqual(end, 7)  # 3-line body: lines 5, 6, 7

    def test_two_consecutive_modules_get_non_overlapping_ranges(self):
        bundle = (
            _module_header("/web/static/lib/luxon/luxon.js", 2)
            + "a\nb\n"
            + _module_header("/ham_shack/static/src/js/web_shack.js", 3)
            + "c\nd\ne\n"
        )
        modules = parse_bundle_module_offsets(bundle)
        self.assertEqual(len(modules), 2)
        (start1, end1, path1), (start2, end2, path2) = modules
        self.assertEqual(path1, "/web/static/lib/luxon/luxon.js")
        self.assertEqual((start1, end1), (5, 6))
        self.assertEqual(path2, "/ham_shack/static/src/js/web_shack.js")
        self.assertGreater(start2, end1)
        self.assertEqual(end2 - start2 + 1, 3)

    def test_a_bundle_with_no_headers_returns_an_empty_list(self):
        self.assertEqual(parse_bundle_module_offsets("plain unbundled content\nno headers here\n"), [])

    def test_a_module_whose_body_contains_the_word_filepath_is_not_mistaken_for_a_new_header(self):
        # Real robustness case for comment-based parsing: a source file's own body could contain
        # the literal word "Filepath" without matching the fixed 4-line star-boxed header shape.
        bundle = (
            _module_header("/web/static/lib/luxon/luxon.js", 2)
            + "// Filepath: not a real header, just a comment mentioning the word\n"
            + "const x = 1;\n"
        )
        modules = parse_bundle_module_offsets(bundle)
        self.assertEqual(len(modules), 1)


class ResolveAddonStaticPathTests(unittest.TestCase):
    def test_resolves_against_the_first_addons_dir_containing_a_real_match(self):
        with tempfile.TemporaryDirectory() as addons_a, tempfile.TemporaryDirectory() as addons_b:
            target_dir = os.path.join(addons_b, "ham_shack", "static", "src", "js")
            os.makedirs(target_dir)
            target_file = os.path.join(target_dir, "web_shack.js")
            with open(target_file, "w", encoding="utf-8") as f:
                f.write("// real file\n")
            resolved = resolve_addon_static_path(
                "/ham_shack/static/src/js/web_shack.js", [addons_a, addons_b]
            )
            self.assertEqual(resolved, target_file)

    def test_returns_none_when_no_addons_dir_has_the_file(self):
        with tempfile.TemporaryDirectory() as addons_a:
            self.assertIsNone(
                resolve_addon_static_path("/nonexistent_module/static/x.js", [addons_a])
            )

    def test_returns_none_for_a_path_with_no_module_segment(self):
        self.assertIsNone(resolve_addon_static_path("/just_one_segment.js", ["/tmp"]))


if __name__ == "__main__":
    unittest.main()
