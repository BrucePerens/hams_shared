#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for check_test_temp_dirs.py against real temporary .rs fixtures."""

import json
import os
import shutil
import tempfile
import unittest

import check_test_temp_dirs as chk  # noqa: E402

HAND_BUILT = 'let d = std::env::temp_dir().join(format!("x_{}", std::process::id()));\n'


class CheckTestTempDirs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_a_hand_built_path_in_a_test_module_is_found(self):
        text = "fn product() {}\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn t() {\n        " + HAND_BUILT + "    }\n}\n"
        self.assertEqual(chk.check_text(text, False), [6])

    def test_the_same_call_in_product_code_before_the_test_module_is_not_judged(self):
        text = "fn product() {\n    " + HAND_BUILT + "}\n#[cfg(test)]\nmod tests {}\n"
        self.assertEqual(chk.check_text(text, False), [])

    def test_a_mod_declaration_with_cfg_test_does_not_make_the_rest_of_the_file_test_code(self):
        text = "#[cfg(test)]\nmod testdir;\nfn product() {\n    " + HAND_BUILT + "}\n"
        self.assertEqual(chk.check_text(text, False), [])

    def test_a_whole_test_file_is_judged_and_a_call_split_over_lines_is_found(self):
        text = "fn t() {\n    let d = std::env::temp_dir()\n        .join(\"x\");\n}\n"
        self.assertEqual(chk.check_text(text, True), [2])

    def test_the_helper_marker_and_the_exception_marker_suppress_with_a_reason(self):
        text = "// temp-dir-helper: Drop removes it\n" + HAND_BUILT + "// temp-dir-ignore: a path that is never created\n" + HAND_BUILT
        self.assertEqual(chk.check_text(text, True), [])
        self.assertEqual(chk.check_text("// temp-dir-helper:\n" + HAND_BUILT, True), [2])

    def test_test_file_names(self):
        for name in ["crate/tests/x.rs", "src/tests.rs", "src/testkit.rs", "src/a_tests.rs", "src/a_test.rs"]:
            self.assertTrue(chk.is_test_file(name.replace("/", os.sep)), name)
        self.assertFalse(chk.is_test_file(os.path.join("src", "main.rs")))

    def test_examples_and_vendored_code_are_skipped_and_the_baseline_ratchets(self):
        self.write("daemons/a/tests/new.rs", "fn t() {\n" + HAND_BUILT + "}\n")
        self.write("daemons/a/examples/demo.rs", "fn t() {\n" + HAND_BUILT + "}\n")
        self.write("daemons/a/vendor/v.rs", "fn t() {\n" + HAND_BUILT + "}\n")
        self.assertEqual(list(chk.scan([self.tmp])), [os.path.join("daemons", "a", "tests", "new.rs")])
        baseline = os.path.join(self.tmp, "baseline.json")
        self.assertEqual(chk.main([self.tmp, "--baseline", baseline]), 1)
        self.assertEqual(chk.main([self.tmp, "--baseline", baseline, "--generate-baseline"]), 0)
        with open(baseline, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {os.path.join("daemons", "a", "tests", "new.rs"): 1})
        self.assertEqual(chk.main([self.tmp, "--baseline", baseline]), 0)
        self.write("daemons/a/tests/new.rs", "fn t() {\n" + HAND_BUILT + HAND_BUILT + "}\n")
        self.assertEqual(chk.main([self.tmp, "--baseline", baseline]), 1, "a second use in a listed file fails")


if __name__ == "__main__":
    unittest.main()
