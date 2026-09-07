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

    def test_a_free_function_with_no_anchor_is_a_gap(self):
        _write(os.path.join(self.tmp, "foo.rs"), "fn bar() {\n    1;\n}\n")
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertIn("foo.rs::bar", gaps)

    def test_a_free_function_with_a_real_anchor_is_not_a_gap(self):
        _write(
            os.path.join(self.tmp, "foo.rs"),
            "// [@ANCHOR: COMM_bar]\nfn bar() {\n    1;\n}\n",
        )
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertNotIn("foo.rs::bar", gaps)

    def test_an_impl_method_gets_a_qualified_type_colon_colon_method_identity(self):
        _write(
            os.path.join(self.tmp, "foo.rs"),
            "struct Foo;\nimpl Foo {\n    fn bar(&self) {\n        1;\n    }\n}\n",
        )
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertIn("foo.rs::Foo::bar", gaps)

    def test_a_trait_default_method_is_a_gap_but_a_bodyless_signature_is_not_scanned(self):
        _write(
            os.path.join(self.tmp, "foo.rs"),
            "trait T {\n    fn required(&self);\n    fn provided(&self) {\n        1;\n    }\n}\n",
        )
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertIn("foo.rs::T::provided", gaps)
        self.assertNotIn("foo.rs::T::required", gaps)

    def test_a_nested_inner_function_is_not_independently_counted(self):
        _write(
            os.path.join(self.tmp, "foo.rs"),
            "fn outer() {\n    fn inner() {\n        1;\n    }\n    inner();\n}\n",
        )
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertIn("foo.rs::outer", gaps)
        self.assertNotIn("foo.rs::inner", gaps)

    def test_a_cfg_test_module_is_excluded_entirely(self):
        _write(
            os.path.join(self.tmp, "foo.rs"),
            "fn real() {\n    1;\n}\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn it_works() {\n        assert!(true);\n    }\n}\n",
        )
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertIn("foo.rs::real", gaps)
        self.assertNotIn("foo.rs::tests::it_works", gaps)

    def test_a_top_level_tests_directory_is_excluded_by_directory_name(self):
        _write(
            os.path.join(self.tmp, "tests", "integration.rs"),
            "fn helper() {\n    1;\n}\n",
        )
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertEqual(gaps, {})

    def test_a_vendor_directory_is_excluded(self):
        _write(
            os.path.join(self.tmp, "vendor", "thirdparty", "src", "lib.rs"),
            "fn third_party() {\n    1;\n}\n",
        )
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertEqual(gaps, {})

    def test_an_examples_directory_is_excluded(self):
        _write(os.path.join(self.tmp, "examples", "demo.rs"), "fn main() {\n    1;\n}\n")
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
            "fn bar() {\n"
            "    // [@ANCHOR-BEGIN: COMM_bar]\n"
            "    1;\n"
            "    // [@ANCHOR-END: COMM_bar]\n"
            "}\n",
        )
        _init_git_repo(self.tmp)
        gaps = crfta.scan_tree(self.tmp)
        self.assertNotIn("foo.rs::bar", gaps)


class BaselineRatchetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.baseline_path = os.path.join(self.tmp, "baseline.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_pre_existing_gap_in_the_baseline_does_not_fail(self):
        repo = os.path.join(self.tmp, "repo")
        _write(os.path.join(repo, "foo.rs"), "fn bar() {\n    1;\n}\n")
        _init_git_repo(repo)
        gaps = crfta.scan_tree(repo)
        crfta.save_baseline(self.baseline_path, gaps)

        baseline = crfta.load_baseline(self.baseline_path)
        current_gaps = crfta.scan_tree(repo)
        new_gaps = [k for k in current_gaps if k not in baseline]
        self.assertEqual(new_gaps, [])

    def test_a_newly_added_unanchored_function_is_a_real_new_gap(self):
        repo = os.path.join(self.tmp, "repo")
        _write(os.path.join(repo, "foo.rs"), "fn bar() {\n    1;\n}\n")
        _init_git_repo(repo)
        crfta.save_baseline(self.baseline_path, crfta.scan_tree(repo))

        _write(
            os.path.join(repo, "foo.rs"),
            "fn bar() {\n    1;\n}\nfn baz() {\n    2;\n}\n",
        )
        baseline = crfta.load_baseline(self.baseline_path)
        current_gaps = crfta.scan_tree(repo)
        new_gaps = [k for k in current_gaps if k not in baseline]
        self.assertEqual(new_gaps, ["foo.rs::baz"])


if __name__ == "__main__":
    unittest.main()
