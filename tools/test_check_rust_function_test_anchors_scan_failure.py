# SPDX-License-Identifier: AGPL-3.0-or-later
"""A Rust scan that did not build or run must fail the check, never pass it vacuously."""
import subprocess
import unittest

import check_rust_function_test_anchors as rust


class ScanFailureTests(unittest.TestCase):
    def _patch(self, name, value):
        original = getattr(rust, name)
        setattr(rust, name, value)
        self.addCleanup(setattr, rust, name, original)

    def test_a_scanner_that_cannot_be_built_fails(self):
        self._patch("_ensure_scan_binary_built", lambda: False)
        with self.assertRaises(SystemExit):
            rust._run_rust_scan(["/x/a.rs"])

    def test_a_scanner_that_exits_non_zero_fails(self):
        self._patch("_ensure_scan_binary_built", lambda: True)
        self._patch("subprocess", type("S", (), {"run": staticmethod(lambda *a, **k: subprocess.CompletedProcess([], 2, "", "boom")), "CalledProcessError": subprocess.CalledProcessError, "PIPE": subprocess.PIPE}))
        with self.assertRaises(SystemExit):
            rust._run_rust_scan(["/x/a.rs"])

    def test_non_json_output_fails(self):
        self._patch("_ensure_scan_binary_built", lambda: True)
        self._patch("subprocess", type("S", (), {"run": staticmethod(lambda *a, **k: subprocess.CompletedProcess([], 0, "oops", "")), "CalledProcessError": subprocess.CalledProcessError, "PIPE": subprocess.PIPE}))
        with self.assertRaises(SystemExit):
            rust._run_rust_scan(["/x/a.rs"])

    def test_no_files_is_an_empty_result(self):
        self.assertEqual(rust._run_rust_scan([]), {})


if __name__ == "__main__":
    unittest.main()
