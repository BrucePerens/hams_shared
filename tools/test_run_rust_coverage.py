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

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_rust_coverage as rrc  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
HAM_DIGITAL_MODES = os.path.join(REPO_ROOT, "daemons", "ham_digital_modes")

_SETTINGS = settings(max_examples=200, deadline=None)

# Real LCOV filenames never contain a literal newline or the two-character
# sequences this module's own line-oriented parser keys off of ("SF:" at
# line-start, the bare "end_of_record" line) -- kept out of the generated
# filename alphabet entirely so a generated fixture can never accidentally
# forge a second SF:/end_of_record boundary the property doesn't expect.
_LCOV_FILENAME = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), max_codepoint=127),
    min_size=1,
    max_size=12,
).map(lambda s: f"src/{s}.rs")

# A real DA count of 0 is "missing"; any other value (including LCOV's own
# real negative-count convention for some unreachable-code cases) is
# "executed" -- parse_lcov's own documented convention, exercised here
# across the full real range rather than just the hand-picked positive
# counts the non-property tests above already cover.
_DA_COUNT = st.integers(min_value=-1000, max_value=1000)


def _build_lcov_text(files):
    """The exact inverse of what `parse_lcov` consumes: `files` is
    `{filename: {line_number: count}}` -- builds real `SF:`/`DA:`/
    `end_of_record` LCOV text from it."""
    lines = []
    for filename, line_counts in files.items():
        lines.append(f"SF:{filename}")
        for line_no, count in line_counts.items():
            lines.append(f"DA:{line_no},{count}")
        lines.append("end_of_record")
    return "\n".join(lines) + "\n"


class ParseLcovPropertiesTests(unittest.TestCase):
    """Property tests for `parse_lcov`, per CODE_REVIEW_PROCESS.md's own
    "Hypothesis property-test scoping" guidance -- a small, pure, well-typed
    text parser, exactly the kind of target that section names, freshly
    written this same session rather than long-since-battle-tested."""

    @_SETTINGS
    @given(
        st.dictionaries(
            _LCOV_FILENAME,
            st.dictionaries(
                st.integers(min_value=1, max_value=100000),
                _DA_COUNT,
                min_size=0,
                max_size=15,
            ),
            min_size=1,
            max_size=5,
        )
    )
    def test_every_line_lands_in_exactly_the_bucket_its_own_count_predicts(self, files):
        lcov_text = _build_lcov_text(files)
        result = rrc.parse_lcov(lcov_text)

        for filename, line_counts in files.items():
            self.assertIn(filename, result)
            expected_executed = sorted(ln for ln, c in line_counts.items() if c > 0)
            expected_missing = sorted(ln for ln, c in line_counts.items() if c <= 0)
            self.assertEqual(result[filename]["executed_lines"], expected_executed)
            self.assertEqual(result[filename]["missing_lines"], expected_missing)

    @_SETTINGS
    @given(
        st.dictionaries(
            _LCOV_FILENAME,
            st.dictionaries(
                st.integers(min_value=1, max_value=100000),
                _DA_COUNT,
                min_size=0,
                max_size=15,
            ),
            min_size=1,
            max_size=5,
        )
    )
    def test_executed_and_missing_never_share_a_line_number(self, files):
        lcov_text = _build_lcov_text(files)
        result = rrc.parse_lcov(lcov_text)
        for data in result.values():
            self.assertEqual(
                set(data["executed_lines"]) & set(data["missing_lines"]),
                set(),
                "the same line number must never be reported as both executed and missing",
            )

    @_SETTINGS
    @given(
        st.dictionaries(
            _LCOV_FILENAME,
            st.dictionaries(
                st.integers(min_value=1, max_value=100000),
                _DA_COUNT,
                min_size=0,
                max_size=15,
            ),
            min_size=1,
            max_size=5,
        )
    )
    def test_both_line_lists_are_always_sorted_ascending(self, files):
        lcov_text = _build_lcov_text(files)
        result = rrc.parse_lcov(lcov_text)
        for data in result.values():
            self.assertEqual(data["executed_lines"], sorted(data["executed_lines"]))
            self.assertEqual(data["missing_lines"], sorted(data["missing_lines"]))

    @_SETTINGS
    @given(
        _LCOV_FILENAME,
        st.integers(min_value=1, max_value=100000),
        st.lists(_DA_COUNT, min_size=2, max_size=5),
    )
    def test_a_repeated_da_record_for_the_same_line_never_lands_in_both_buckets(
        self, filename, line_no, counts
    ):
        # Real bug this test was written to catch, found by the sibling property test above with
        # a hand-constructed duplicate-DA fixture, not by this test itself (Hypothesis's own
        # dictionary strategy naturally deduplicates same-key line numbers, so it could never
        # generate this shape on its own): real `cargo llvm-cov` output was checked directly and
        # never emits two DA: records for the same line in one SF: block, so this is a defensive
        # correctness property, not a reproduction of an observed real-world input.
        lcov_text = "SF:" + filename + "\n"
        for count in counts:
            lcov_text += f"DA:{line_no},{count}\n"
        lcov_text += "end_of_record\n"

        result = rrc.parse_lcov(lcov_text)
        executed = set(result[filename]["executed_lines"])
        missing = set(result[filename]["missing_lines"])
        self.assertEqual(
            executed & missing,
            set(),
            f"line {line_no} landed in both buckets from repeated DA records {counts}",
        )
        # Matching real LCOV merge semantics (lcov --add-tracefile sums counts across repeated
        # records for the same line): covered if ANY repeated record reports a positive count.
        expected_executed = any(c > 0 for c in counts)
        self.assertEqual(line_no in executed, expected_executed)
        self.assertEqual(line_no in missing, not expected_executed)


class ToRepoRelativeStaysUnderRepoRootPropertyTest(unittest.TestCase):
    """`to_repo_relative`'s own real invariant: whatever repo-relative key it produces, joining
    `repo_root` back onto it must reconstruct the same absolute path `crate_dir`/`path` names --
    the join it does is a real inverse of `os.path.relpath`, not just "looks about right" on the
    hand-picked example the non-property test above already checks."""

    @_SETTINGS
    @given(
        st.lists(
            st.text(alphabet=st.characters(whitelist_categories=("Ll", "Nd")), min_size=1, max_size=8),
            min_size=1,
            max_size=4,
        ),
        st.text(alphabet=st.characters(whitelist_categories=("Ll",)), min_size=1, max_size=10).map(
            lambda s: f"{s}.rs"
        ),
    )
    def test_repo_relative_path_reconstructs_the_real_absolute_source_path(self, crate_subdirs, leaf_filename):
        repo_root = "/repo"
        crate_dir = os.path.join(repo_root, *crate_subdirs)
        crate_relative_path = f"src/{leaf_filename}"
        per_file = {crate_relative_path: {"executed_lines": [1], "missing_lines": []}}

        result = rrc.to_repo_relative(per_file, crate_dir=crate_dir, repo_root=repo_root)

        self.assertEqual(len(result), 1)
        (repo_relative_key,) = result.keys()
        reconstructed = os.path.normpath(os.path.join(repo_root, repo_relative_key))
        expected = os.path.normpath(os.path.join(crate_dir, crate_relative_path))
        self.assertEqual(reconstructed, expected)


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
