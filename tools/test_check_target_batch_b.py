#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Negative controls for batch B of the check_target conversion (JavaScript, XML, SCSS and documentation checkers): each one exits 2 with
the cause when its target is missing, is a file, or (opt-out unset) holds none of the files it reads.
"""

import os
import subprocess
import tempfile
import unittest

import test_check_target as shared

CHECKERS = (
    "check_js_syntax.py",
    "check_js_function_test_anchors.py",
    "check_js_test_hook_gating.py",
    "check_minified_js_nested_templates.py",
    "check_minified_js_syntax_errors.py",
    "check_hoot_runner_coverage.py",
    "check_window_fetch_reassignment.py",
    "check_owl_templates.py",
    "check_scss_math_functions.py",
    "check_xml_attribute_position_comment.py",
    "check_xml_comment_double_hyphen.py",
    "check_external_library_locality.py",
    "check_doc_images.py",
)


class BatchBRefuseABadTargetTests(unittest.TestCase):
    def test_every_checker_refuses_a_missing_path_a_file_and_an_empty_directory(self):
        for script in CHECKERS:
            with self.subTest(checker=script):
                shared.assert_checker_refuses_a_bad_target(self, script)

    def test_noupdate_edits_refuses_a_missing_path_a_file_and_a_directory_that_is_not_a_git_work_tree(self):
        # It reads a git diff, where "no changed file" is normal, so it has no empty-scan rule; a tree git cannot diff is not a pass.
        shared.assert_checker_refuses_a_bad_target(self, "check_noupdate_edits.py", empty=False)
        with tempfile.TemporaryDirectory() as tmp:
            done = shared.run_checker("check_noupdate_edits.py", tmp, cwd=tmp)
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
            self.assertIn("not inside a git work tree", done.stderr)

    def test_noupdate_edits_refuses_a_base_ref_git_cannot_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            done = shared.run_checker("check_noupdate_edits.py", tmp, "--base", "no_such_ref", cwd=tmp)
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
            self.assertIn("cannot diff", done.stderr)

    def test_js_syntax_with_one_good_directory_and_one_missing_still_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "good")
            os.makedirs(good)
            with open(os.path.join(good, "a.js"), "w", encoding="utf-8") as handle:
                handle.write("var a = 1;\n")
            done = shared.run_checker("check_js_syntax.py", good, os.path.join(tmp, "missing"), cwd=tmp)
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
            self.assertIn("does not exist", done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
