#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_rust_function_test_anchors.py (ADR 0090 decision 2's Rust sub-track).

Real, non-mocked `cargo`/`rust_function_scan` invocations throughout -- same "use the real tool,
don't mock it" precedent test_check_js_function_test_anchors.py's own module doc comment already
establishes for the JS sub-track, since this is exactly the integration point
(`rust_function_scan`'s own subprocess boundary) most likely to break in a way a mock would hide.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_rust_function_test_anchors as crfta  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _init_git_repo(root):
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)


class ScanTreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # A single bare-literal-statement body (`fn bar() { 1; }`) is exactly
    # what the Stage 1 size/shape rule (`is_trivial`, see rust_function_
    # scan's own doc comment) is designed to exclude -- these fixtures
    # deliberately use a real `if` so the anchor-detection logic under
    # test isn't confounded by the separate trivial-exclusion rule,
    # which has its own dedicated tests below.
    _NONTRIVIAL_BODY = "if x > 0 {\n        x\n    } else {\n        -x\n    }"

    def test_a_free_function_with_no_anchor_is_a_gap(self):
        _write(
            os.path.join(self.tmp, "foo.rs"),
            f"fn bar(x: i32) -> i32 {{\n    {self._NONTRIVIAL_BODY}\n}}\n",
        )
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertIn("foo.rs::bar", gaps)

    def test_a_free_function_with_a_real_anchor_is_not_a_gap(self):
        _write(
            os.path.join(self.tmp, "foo.rs"),
            f"// [@ANCHOR: COMM_bar]\nfn bar(x: i32) -> i32 {{\n    {self._NONTRIVIAL_BODY}\n}}\n",
        )
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertNotIn("foo.rs::bar", gaps)

    def test_a_verified_by_citation_with_no_real_base_anchor_is_still_a_gap(self):
        # Same defect as the Python scanner's own real bug (see
        # check_function_test_anchors.py's `_is_base_anchor_declaration`): a bare
        # `ANCHOR_PATTERN.search` over the joined span would count a
        # `// Verified by [@ANCHOR: ...]` citation as a real base declaration,
        # silently exempting the function.
        _write(
            os.path.join(self.tmp, "foo.rs"),
            f"// Verified by [@ANCHOR: mod:test_bar]\nfn bar(x: i32) -> i32 {{\n    {self._NONTRIVIAL_BODY}\n}}\n",
        )
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertIn("foo.rs::bar", gaps)

    def test_an_impl_method_gets_a_qualified_type_colon_colon_method_identity(self):
        _write(
            os.path.join(self.tmp, "foo.rs"),
            "struct Foo;\nimpl Foo {\n"
            f"    fn bar(&self, x: i32) -> i32 {{\n        {self._NONTRIVIAL_BODY}\n    }}\n}}\n",
        )
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertIn("foo.rs::Foo::bar", gaps)

    def test_a_trait_default_method_is_a_gap_but_a_bodyless_signature_is_not_scanned(self):
        _write(
            os.path.join(self.tmp, "foo.rs"),
            "trait T {\n    fn required(&self);\n"
            f"    fn provided(&self, x: i32) -> i32 {{\n        {self._NONTRIVIAL_BODY}\n    }}\n}}\n",
        )
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertIn("foo.rs::T::provided", gaps)
        self.assertNotIn("foo.rs::T::required", gaps)

    def test_a_nested_inner_function_is_not_independently_counted(self):
        # `outer`'s own top-level body must have real control flow of its
        # own, not just delegate to `inner` -- a bare "define a helper,
        # call it" body has no branching at its own level and would
        # (correctly) read as trivial under the size/shape rule, which
        # isn't what this test means to exercise (nested-function
        # counting, not trivial-exclusion).
        _write(
            os.path.join(self.tmp, "foo.rs"),
            "fn outer(x: i32) -> i32 {\n"
            "    fn inner(y: i32) -> i32 {\n"
            f"        {self._NONTRIVIAL_BODY}\n"
            "    }\n"
            "    if x > 0 {\n"
            "        inner(x)\n"
            "    } else {\n"
            "        inner(-x)\n"
            "    }\n"
            "}\n",
        )
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertIn("foo.rs::outer", gaps)
        self.assertNotIn("foo.rs::inner", gaps)

    def test_a_cfg_test_module_is_excluded_entirely(self):
        _write(
            os.path.join(self.tmp, "foo.rs"),
            f"fn real(x: i32) -> i32 {{\n    {self._NONTRIVIAL_BODY}\n}}\n#[cfg(test)]\nmod tests {{\n    #[test]\n    fn it_works() {{\n        assert!(true);\n    }}\n}}\n",
        )
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertIn("foo.rs::real", gaps)
        self.assertNotIn("foo.rs::tests::it_works", gaps)

    def test_a_top_level_tests_directory_is_excluded_by_directory_name(self):
        _write(
            os.path.join(self.tmp, "tests", "integration.rs"),
            f"fn helper(x: i32) -> i32 {{\n    {self._NONTRIVIAL_BODY}\n}}\n",
        )
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertEqual(gaps, {})

    def test_a_vendor_directory_is_excluded(self):
        _write(
            os.path.join(self.tmp, "vendor", "thirdparty", "src", "lib.rs"),
            f"fn third_party(x: i32) -> i32 {{\n    {self._NONTRIVIAL_BODY}\n}}\n",
        )
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertEqual(gaps, {})

    def test_an_examples_directory_is_excluded(self):
        _write(
            os.path.join(self.tmp, "examples", "demo.rs"),
            f"fn main() {{\n    let x = 1;\n    {self._NONTRIVIAL_BODY};\n}}\n",
        )
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertEqual(gaps, {})

    def test_a_syntax_error_file_is_skipped_not_crashed_on(self):
        _write(os.path.join(self.tmp, "broken.rs"), "fn broken( {\n    1\n")
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertEqual(gaps, {})

    def test_a_begin_end_anchor_counts_as_anchored(self):
        _write(
            os.path.join(self.tmp, "foo.rs"),
            f"fn bar(x: i32) -> i32 {{\n"
            "    // [@ANCHOR-BEGIN: COMM_bar]\n"
            f"    {self._NONTRIVIAL_BODY}\n"
            "    // [@ANCHOR-END: COMM_bar]\n"
            "}\n",
        )
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertNotIn("foo.rs::bar", gaps)

    def test_a_trivial_single_expression_function_is_never_a_gap_even_unanchored(self):
        # The Stage 1 size/shape rule's own real-world calibration case
        # (rust_function_scan's own test suite): a bare tail-expression
        # function, no anchor at all -- must never show up as a gap,
        # not just "a low-priority one."
        _write(
            os.path.join(self.tmp, "foo.rs"),
            "fn f0_to_wo(f0: f32) -> f32 {\n    std::f32::consts::TAU * f0 / 8000.0\n}\n",
        )
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertEqual(gaps, {})

    def test_a_let_plus_debug_assert_plus_tail_expression_is_still_trivial(self):
        _write(
            os.path.join(self.tmp, "foo.rs"),
            "fn rshift_round(x: i64, n: u32) -> i64 {\n"
            "    let shifted = x >> n;\n"
            '    debug_assert!(shifted >= 0, "must be non-negative");\n'
            "    shifted\n"
            "}\n",
        )
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertEqual(gaps, {})

    def test_a_function_with_real_control_flow_is_a_gap_even_though_its_short(self):
        # Real property: trivial-exclusion is about control-flow/statement
        # SHAPE, not raw line count -- a short function with a real `if`
        # still needs a real anchor.
        _write(
            os.path.join(self.tmp, "foo.rs"),
            "fn clamp(x: f32) -> f32 {\n    if x > 1.0 { 1.0 } else { x }\n}\n",
        )
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertIn("foo.rs::clamp", gaps)


class BaselineRatchetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.baseline_path = os.path.join(self.tmp, "baseline.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    _NONTRIVIAL_BODY = "if x > 0 {\n        x\n    } else {\n        -x\n    }"

    def test_a_pre_existing_gap_in_the_baseline_does_not_fail(self):
        repo = os.path.join(self.tmp, "repo")
        _write(
            os.path.join(repo, "foo.rs"),
            f"fn bar(x: i32) -> i32 {{\n    {self._NONTRIVIAL_BODY}\n}}\n",
        )
        _init_git_repo(repo)
        gaps = crfta.scan_tree(repo)
        self.assertIn("foo.rs::bar", gaps, "fixture must produce a real gap for this test to mean anything")
        crfta.save_baseline(self.baseline_path, gaps)

        baseline = crfta.load_baseline(self.baseline_path)
        current_gaps = crfta.scan_tree(repo)
        new_gaps = [k for k in current_gaps if k not in baseline]
        self.assertEqual(new_gaps, [])

    def test_a_newly_added_unanchored_function_is_a_real_new_gap(self):
        repo = os.path.join(self.tmp, "repo")
        _write(
            os.path.join(repo, "foo.rs"),
            f"fn bar(x: i32) -> i32 {{\n    {self._NONTRIVIAL_BODY}\n}}\n",
        )
        _init_git_repo(repo)
        crfta.save_baseline(self.baseline_path, crfta.scan_tree(repo))

        _write(
            os.path.join(repo, "foo.rs"),
            f"fn bar(x: i32) -> i32 {{\n    {self._NONTRIVIAL_BODY}\n}}\n"
            f"fn baz(x: i32) -> i32 {{\n    {self._NONTRIVIAL_BODY}\n}}\n",
        )
        baseline = crfta.load_baseline(self.baseline_path)
        current_gaps = crfta.scan_tree(repo)
        new_gaps = [k for k in current_gaps if k not in baseline]
        self.assertEqual(new_gaps, ["foo.rs::baz"])


if __name__ == "__main__":
    unittest.main()
