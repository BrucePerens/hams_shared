#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for run_rust_coverage.py (ANCHOR_COVERAGE_AND_REMEDIATION_PLAN.md Stage 2's Rust
sub-track).

`parse_lcov`/`to_repo_relative`/`find_repo_root` are tested against real, hand-verified fixture
data (LCOV is a simple, stable text format -- no real subprocess boundary to mock there). The one
real subprocess integration point, `run_llvm_cov_lcov`, is exercised by a real, non-mocked
`cargo llvm-cov` invocation against this repo's own real `ham_digital_modes` crate -- the same
"use the real tool, don't mock it" precedent this codebase's other scanner test suites already
establish, and the acceptance test the Python coverage sub-track's own BUILT note used
(a real, already-anchored module, not a synthetic fixture).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_rust_coverage as rrc  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
HAM_DIGITAL_MODES = os.path.join(REPO_ROOT, "daemons", "ham_digital_modes")


class ParseLcovTests(unittest.TestCase):
    def test_a_single_file_with_mixed_executed_and_missing_lines(self):
        lcov = (
            "SF:src/foo.rs\n"
            "DA:1,5\n"
            "DA:2,0\n"
            "DA:3,12\n"
            "DA:4,0\n"
            "end_of_record\n"
        )
        result = rrc.parse_lcov(lcov)
        self.assertEqual(result, {"src/foo.rs": {"executed_lines": [1, 3], "missing_lines": [2, 4]}})

    def test_multiple_files_stay_separate(self):
        lcov = (
            "SF:src/a.rs\n"
            "DA:1,1\n"
            "end_of_record\n"
            "SF:src/b.rs\n"
            "DA:1,0\n"
            "end_of_record\n"
        )
        result = rrc.parse_lcov(lcov)
        self.assertEqual(result["src/a.rs"]["executed_lines"], [1])
        self.assertEqual(result["src/b.rs"]["missing_lines"], [1])

    def test_a_file_with_no_da_records_reports_no_lines_either_way(self):
        lcov = "SF:src/empty.rs\nend_of_record\n"
        result = rrc.parse_lcov(lcov)
        self.assertEqual(result, {"src/empty.rs": {"executed_lines": [], "missing_lines": []}})

    def test_lines_come_back_sorted_even_if_the_lcov_text_is_not(self):
        lcov = "SF:src/foo.rs\nDA:9,1\nDA:2,1\nDA:5,0\nend_of_record\n"
        result = rrc.parse_lcov(lcov)
        self.assertEqual(result["src/foo.rs"]["executed_lines"], [2, 9])
        self.assertEqual(result["src/foo.rs"]["missing_lines"], [5])


class ToRepoRelativeTests(unittest.TestCase):
    def test_prepends_the_crates_own_path_relative_to_repo_root(self):
        per_file = {"src/ambe/decode.rs": {"executed_lines": [1], "missing_lines": []}}
        result = rrc.to_repo_relative(
            per_file,
            crate_dir="/repo/daemons/ham_digital_modes",
            repo_root="/repo",
        )
        self.assertEqual(
            result,
            {"daemons/ham_digital_modes/src/ambe/decode.rs": {"executed_lines": [1], "missing_lines": []}},
        )

    def test_a_crate_directly_at_repo_root_produces_a_bare_relative_path(self):
        per_file = {"src/lib.rs": {"executed_lines": [], "missing_lines": [1]}}
        result = rrc.to_repo_relative(per_file, crate_dir="/repo", repo_root="/repo")
        self.assertEqual(result, {"src/lib.rs": {"executed_lines": [], "missing_lines": [1]}})


class FindRepoRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_a_git_directory_at_the_starting_point(self):
        os.makedirs(os.path.join(self.tmp, ".git"))
        self.assertEqual(rrc.find_repo_root(self.tmp), self.tmp)

    def test_walks_upward_from_a_nested_crate_directory(self):
        os.makedirs(os.path.join(self.tmp, ".git"))
        nested = os.path.join(self.tmp, "daemons", "some_crate")
        os.makedirs(nested)
        self.assertEqual(rrc.find_repo_root(nested), self.tmp)

    def test_returns_none_when_no_git_directory_exists_anywhere_above(self):
        nested = os.path.join(self.tmp, "a", "b", "c")
        os.makedirs(nested)
        # tempfile.mkdtemp() lives under a real filesystem root that itself has no .git --
        # real assertion, not assumed, since this test would silently pass for the wrong reason
        # on a machine where some ancestor of /tmp genuinely does contain a .git directory.
        self.assertIsNone(rrc.find_repo_root(nested))


class RealCargoLlvmCovAcceptanceTest(unittest.TestCase):
    """The real end-to-end acceptance test: runs cargo llvm-cov against this repo's own real
    ham_digital_modes crate (skipped if that crate isn't checked out where expected, e.g. running
    these tests from a bare hams_shared clone with no sibling hams_open checkout)."""

    @unittest.skipUnless(
        os.path.isfile(os.path.join(HAM_DIGITAL_MODES, "Cargo.toml")),
        "ham_digital_modes crate not found relative to this checkout",
    )
    def test_a_real_release_build_produces_a_real_repo_relative_report(self):
        # Deliberately --release (this script's own default), not --debug-build: an earlier draft
        # of this test used a debug build and a real run exceeded a 400s timeout without even
        # finishing -- this crate's own DSP-heavy tests (FFTs, real off-air signal decodes) run
        # meaningfully slower unoptimized. The manual spike this tool was designed against
        # (--release, full suite) completed in ~20s, matching what's used here.
        proc = subprocess.run(["which", "cargo-llvm-cov"], capture_output=True)
        if proc.returncode != 0:
            self.skipTest("cargo-llvm-cov not installed")

        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "rust_coverage.json")
            argv = [
                "run_rust_coverage.py",
                HAM_DIGITAL_MODES,
                "--repo-root",
                REPO_ROOT,
                "--output",
                output_path,
            ]
            old_argv = sys.argv
            sys.argv = argv
            try:
                exit_code = rrc.main()
            finally:
                sys.argv = old_argv

            self.assertEqual(exit_code, 0)
            with open(output_path, "r", encoding="utf-8") as f:
                report = json.load(f)

            decode_key = "daemons/ham_digital_modes/src/ambe/decode.rs"
            self.assertIn(
                decode_key,
                report["files"],
                "expected decode.rs to appear under its real repo-relative path",
            )
            self.assertGreater(
                len(report["files"][decode_key]["executed_lines"]),
                0,
                "decode.rs is exercised by real tests -- expected at least some executed lines",
            )


if __name__ == "__main__":
    unittest.main()
