#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The repository-pair checkers scan several roots in one process (one run_linters.py step)."""

import os
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.dirname(os.path.abspath(__file__))

BAD_FLOAT = "def f(text):\n    try:\n        return float(text)\n    except ValueError:\n        return 0.0\n"
BAD_INT = "def f(text):\n    return int(float(text))\n"
FETCH_BAD = "window.fetch = () => 1;\n"


def _run(script, *roots):
    return subprocess.run(
        [sys.executable, os.path.join(TOOLS, script), *roots], capture_output=True, text=True, timeout=120
    )


class MultiRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="multiroot-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.first = os.path.join(self.tmp, "hams_com")
        self.second = os.path.join(self.tmp, "hams_open")
        os.makedirs(self.first)
        os.makedirs(self.second)

    def write(self, root, relative, text):
        path = os.path.join(root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_a_finding_in_either_root_fails_the_one_run_and_names_the_repository(self):
        for script, bad in (("check_float_isfinite.py", BAD_FLOAT), ("check_int_float_overflow.py", BAD_INT)):
            for bad_root, other in ((self.first, self.second), (self.second, self.first)):
                self.write(bad_root, "mod/x.py", bad)
                self.write(other, "mod/y.py", "VALUE = 1\n")
                result = _run(script, self.first, self.second)
                self.assertEqual(result.returncode, 1, script + result.stdout + result.stderr)
                self.assertIn(os.path.join(os.path.basename(bad_root), "mod", "x.py"), result.stdout)
                os.unlink(os.path.join(bad_root, "mod", "x.py"))

    def test_a_clean_pair_passes_and_a_single_root_still_works(self):
        for script in (
            "check_float_isfinite.py",
            "check_int_float_overflow.py",
            "check_async_lock_scope.py",
            "check_fake_network_listener.py",
            "check_window_fetch_reassignment.py",
        ):
            self.write(self.first, "mod/ok.py", "VALUE = 1\n")
            # check_async_lock_scope reads Rust only: a scan of no Rust file is refused, so the clean tree holds one.
            self.write(self.first, "mod/ok.rs", "fn main() {}\n")
            # check_window_fetch_reassignment reads *.test.js only.
            self.write(self.first, "mod/ok.test.js", "const value = 1;\n")
            self.assertEqual(_run(script, self.first, self.second).returncode, 0, script)
            self.assertEqual(_run(script, self.first).returncode, 0, script)

    def test_window_fetch_reassignment_in_the_second_root_is_found(self):
        self.write(self.second, "ham_x/static/tests/a.test.js", FETCH_BAD)
        result = _run("check_window_fetch_reassignment.py", self.first, self.second)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("a.test.js", result.stdout)

    def test_no_argument_is_a_usage_error(self):
        for script in ("check_float_isfinite.py", "check_async_lock_scope.py", "check_window_fetch_reassignment.py"):
            self.assertEqual(_run(script).returncode, 1, script)


if __name__ == "__main__":
    unittest.main()
