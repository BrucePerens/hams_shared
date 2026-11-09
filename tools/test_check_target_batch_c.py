#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Negative controls for the batch C checkers converted to check_target.py (Rust, supply-chain, manifest and hook checkers): a missing
target, a plain file, or an empty directory is exit 2 with the cause named, never a pass of zero files.
"""

import os
import subprocess
import tempfile
import unittest

from test_check_target import assert_checker_refuses_a_bad_target, run_checker

HERE = os.path.dirname(os.path.abspath(__file__))

# Each takes a directory as its first argument and exits 2 on a missing path, a file and (unless the opt-out is set) an empty directory.
DIRECTORY_CHECKERS = (
    "check_cargo_clippy.py",
    "check_cargo_deny.py",
    "check_cargo_fmt.py",
    "check_rust_function_test_anchors.py",
    "check_external_programs_documented.py",
    "check_pip_audit.py",
    "check_skill_integrity.py",
    "check_burn_list.py",
    "check_binary_manifest_consistency.py",
    "check_test_temp_dirs.py",
)


class BatchCTests(unittest.TestCase):
    def test_every_directory_checker_refuses_a_bad_target(self):
        for script in DIRECTORY_CHECKERS:
            with self.subTest(script=script):
                assert_checker_refuses_a_bad_target(self, script)

    def test_check_claude_hook_registration_refuses_a_bad_target(self):
        # Its empty-directory case is a finding (no settings file, exit 1), not a zero-file scan, so only the target is checked.
        assert_checker_refuses_a_bad_target(self, "check_claude_hook_registration.py", empty=False)

    def test_check_claims_freshness_refuses_a_bad_target(self):
        # Already exit 2 for a tree git cannot list (hams_shared #569); kept here as the control.
        with tempfile.TemporaryDirectory() as tmp:
            for target in (os.path.join(tmp, "gone"), tmp):
                done = run_checker("check_claims_freshness.py", target, cwd=tmp)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)

    def test_a_burn_list_module_colon_test_class_target_is_still_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            done = run_checker("check_burn_list.py", os.path.join(tmp, "some_module:TestThing"), cwd=tmp)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_check_dependency_releases_refuses_a_missing_file_a_directory_and_an_empty_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            done = run_checker("check_dependency_releases.py", os.path.join(tmp, "gone.json"), cwd=tmp)
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
            self.assertIn("does not exist", done.stderr)
            done = run_checker("check_dependency_releases.py", tmp, cwd=tmp)
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
            self.assertIn("not a file", done.stderr)
            empty = os.path.join(tmp, "empty.json")
            with open(empty, "w", encoding="utf-8") as handle:
                handle.write('{"_comment": "no entries"}\n')
            done = run_checker("check_dependency_releases.py", empty, cwd=tmp)
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
            self.assertIn("scanned 0", done.stderr)

    def test_a_skill_checkout_with_no_skill_files_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "agents", "skills"))
            done = run_checker("check_skill_integrity.py", tmp, cwd=tmp)
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)

    def test_check_summation_bias_outside_a_git_repository_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            done = run_checker("check_summation_bias.py", cwd=tmp)
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
            self.assertIn("git could not list", done.stderr)

    def test_check_summation_bias_in_a_repository_with_no_commit_yet_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            done = run_checker("check_summation_bias.py", cwd=tmp)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_a_tree_with_one_rust_crate_still_passes_the_crate_count(self):
        # Positive control: the zero-crate failure is about zero, not about a small tree.
        with tempfile.TemporaryDirectory() as tmp:
            crate = os.path.join(tmp, "a_crate")
            os.makedirs(os.path.join(crate, "src"))
            with open(os.path.join(crate, "Cargo.toml"), "w", encoding="utf-8") as handle:
                handle.write('[package]\nname = "a"\nversion = "0.1.0"\n')
            done = run_checker("check_cargo_deny.py", tmp, cwd=tmp)
            # A crate without a deny.toml is not one cargo-deny covers: still zero for this checker.
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
            with open(os.path.join(crate, "deny.toml"), "w", encoding="utf-8") as handle:
                handle.write("")
            done = run_checker("check_cargo_deny.py", tmp, cwd=tmp)
            self.assertNotIn("scanned 0", done.stderr)


if __name__ == "__main__":
    unittest.main()
