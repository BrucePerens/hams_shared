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

from hypothesis import given, settings
from hypothesis import strategies as st

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


# Filepath alphabet restricted to what a real Odoo addon-relative static path actually looks
# like (letters, digits, /, _, ., -) -- deliberately excludes whitespace/backslash/'*' so
# Hypothesis can't accidentally generate a filepath string that itself looks like part of the
# fixed header shape (which would change how many headers the regex matches, a different
# property than the one under test here).
_FILEPATH_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/_.-"
_filepaths = st.text(alphabet=_FILEPATH_ALPHABET, min_size=1, max_size=40)
# Body lines restricted to plain alphanumeric text -- real module bodies can contain almost
# anything, but this property is about offset/line-count arithmetic, not about every possible
# body content; the "body contains the literal word Filepath" robustness case is already covered
# separately, by example, above.
_body_lines = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789 ", min_size=0, max_size=20)


@st.composite
def _bundle_with_known_modules(draw):
    """Builds a real-shaped bundle (an optional random noise prefix, then 1-5 modules each with a
    real header and a body of exactly its declared line count) and returns
    `(bundle_text, expected)` where `expected` is the list of `(line_count, filepath)` pairs the
    parser should recover, in order."""
    prefix_lines = draw(st.lists(_body_lines, min_size=0, max_size=5))
    bundle = "".join(line + "\n" for line in prefix_lines)
    expected = []
    num_modules = draw(st.integers(min_value=1, max_value=5))
    for _ in range(num_modules):
        filepath = "/" + draw(_filepaths)
        body = draw(st.lists(_body_lines, min_size=1, max_size=15))
        bundle += _module_header(filepath, len(body))
        bundle += "".join(line + "\n" for line in body)
        expected.append((len(body), filepath))
    return bundle, expected


class ParseBundleModuleOffsetsPropertyTests(unittest.TestCase):
    """Hypothesis property tests, added per CODE_REVIEW_PROCESS.md's own standing "more Hypothesis
    property-test scoping" guidance -- this parser is exactly the kind of small, pure, well-typed
    target that section calls out, and shares its regex-header-parsing shape with
    `run_rust_coverage.py`'s `parse_lcov`, where the same kind of property test already caught a
    real bug (counts landing in both executed/missing buckets for a duplicate DA: record)."""

    @given(_bundle_with_known_modules())
    @settings(max_examples=200)
    def test_recovers_exactly_the_declared_modules_with_correct_line_counts_and_no_overlap(
        self, bundle_and_expected
    ):
        bundle, expected = bundle_and_expected
        modules = parse_bundle_module_offsets(bundle)
        self.assertEqual(len(modules), len(expected))
        prev_end = None
        for (start, end, filepath), (expected_line_count, expected_filepath) in zip(
            modules, expected
        ):
            self.assertEqual(filepath, expected_filepath)
            # The real invariant this parser exists to guarantee: a module's reported range has
            # exactly as many lines as its own header declared, regardless of what other modules,
            # or how much noise, precede it in the bundle.
            self.assertEqual(end - start + 1, expected_line_count)
            if prev_end is not None:
                self.assertGreater(start, prev_end)
            prev_end = end

    @given(st.integers(min_value=1, max_value=200))
    @settings(max_examples=50)
    def test_a_single_modules_range_never_includes_its_own_header_lines(self, line_count):
        filepath = "/web/static/src/js/some_module.js"
        header = _module_header(filepath, line_count)
        header_line_count = header.count("\n")
        body = "".join(f"line {i}\n" for i in range(line_count))
        modules = parse_bundle_module_offsets(header + body)
        self.assertEqual(len(modules), 1)
        start, end, _ = modules[0]
        self.assertEqual(start, header_line_count + 1)
        self.assertEqual(end, header_line_count + line_count)


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
