#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_cargo_clippy.py.

main()'s actual `cargo clippy` invocation is a real, potentially slow subprocess call -- never
exercised for real here. subprocess.run is mocked throughout main()'s tests, the same
boundary-mocking approach test_check_cargo_deny.py/test_check_pip_audit.py use for their own
subprocess calls. find_cargo_crates() has no subprocess involvement at all and is tested
directly against real temp-directory fixtures.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_cargo_clippy as chk  # noqa: E402


def _write(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class FindCargoCratesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_package_crate_is_found(self):
        crate = os.path.join(self.tmp, "daemons", "some_daemon")
        _write(os.path.join(crate, "Cargo.toml"), "[package]\nname = \"some_daemon\"\n")
        self.assertEqual(chk.find_cargo_crates(self.tmp), [crate])

    def test_a_cargo_toml_without_a_package_table_is_not_found(self):
        # A workspace-only Cargo.toml (no [package] table of its own) --
        # its member crates are found and checked individually instead.
        crate = os.path.join(self.tmp, "workspace_root")
        _write(os.path.join(crate, "Cargo.toml"), "[workspace]\nmembers = [\"a\"]\n")
        self.assertEqual(chk.find_cargo_crates(self.tmp), [])

    def test_an_ignored_directory_is_never_walked(self):
        crate = os.path.join(self.tmp, "node_modules", "pkg")
        _write(os.path.join(crate, "Cargo.toml"), "[package]\nname = \"pkg\"\n")
        self.assertEqual(chk.find_cargo_crates(self.tmp), [])

    def test_multiple_crates_are_all_found_and_sorted(self):
        crate_a = os.path.join(self.tmp, "daemons", "a_daemon")
        crate_b = os.path.join(self.tmp, "daemons", "b_daemon")
        for crate in (crate_a, crate_b):
            _write(os.path.join(crate, "Cargo.toml"), "[package]\nname = \"x\"\n")
        found = chk.find_cargo_crates(self.tmp)
        self.assertEqual(len(found), 2)
        self.assertEqual(found, sorted(found))


class MainTests(unittest.TestCase):
    """subprocess.run is mocked throughout -- no real `cargo clippy` invocation is ever made in
    these tests."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _argv(self):
        return ["check_cargo_clippy.py", self.tmp]

    def _make_crate(self):
        crate = os.path.join(self.tmp, "daemons", "some_daemon")
        _write(os.path.join(crate, "Cargo.toml"), "[package]\nname = \"some_daemon\"\n")
        return crate

    def test_no_crates_exits_0_without_calling_subprocess_at_all(self):
        with patch.object(sys, "argv", self._argv()), patch(
            "check_cargo_clippy.subprocess.run"
        ) as mock_run:
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 0)
            mock_run.assert_not_called()

    def test_clippy_not_installed_exits_1_with_a_clear_message(self):
        self._make_crate()
        version_check = MagicMock(returncode=1)
        with patch.object(sys, "argv", self._argv()), patch(
            "check_cargo_clippy.subprocess.run", return_value=version_check
        ) as mock_run, patch("builtins.print") as mock_print:
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 1)
            self.assertEqual(mock_run.call_count, 1)
            printed = " ".join(str(c) for c in mock_print.call_args_list)
            self.assertIn("not installed", printed)

    def test_a_clean_pass_exits_0(self):
        self._make_crate()
        version_check = MagicMock(returncode=0)
        clippy_result = MagicMock(returncode=0, stdout="", stderr="    Finished `dev` profile")
        with patch.object(sys, "argv", self._argv()), patch(
            "check_cargo_clippy.subprocess.run", side_effect=[version_check, clippy_result]
        ):
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 0)

    def test_a_warning_in_stderr_exits_1_even_with_returncode_0(self):
        # clippy's own exit code stays 0 for plain warnings (no -D passed) --
        # this gate must catch that case by scanning stderr, not just returncode.
        self._make_crate()
        version_check = MagicMock(returncode=0)
        clippy_result = MagicMock(returncode=0, stdout="", stderr="warning: unused variable")
        with patch.object(sys, "argv", self._argv()), patch(
            "check_cargo_clippy.subprocess.run", side_effect=[version_check, clippy_result]
        ):
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 1)

    def test_a_nonzero_returncode_exits_1_even_with_no_warning_text(self):
        # A real build error (not just a lint warning) -- must not be
        # silently swallowed just because "warning:" didn't appear.
        self._make_crate()
        version_check = MagicMock(returncode=0)
        clippy_result = MagicMock(returncode=1, stdout="", stderr="error[E0433]: failed to resolve")
        with patch.object(sys, "argv", self._argv()), patch(
            "check_cargo_clippy.subprocess.run", side_effect=[version_check, clippy_result]
        ):
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 1)

    def test_multiple_crates_are_all_checked_even_after_a_finding(self):
        crate_a = os.path.join(self.tmp, "daemons", "a_daemon")
        crate_b = os.path.join(self.tmp, "daemons", "b_daemon")
        for crate in (crate_a, crate_b):
            _write(os.path.join(crate, "Cargo.toml"), "[package]\nname = \"x\"\n")
        version_check = MagicMock(returncode=0)
        finding = MagicMock(returncode=0, stdout="", stderr="warning: unused variable")
        clean = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(sys, "argv", self._argv()), patch(
            "check_cargo_clippy.subprocess.run",
            side_effect=[version_check, finding, clean],
        ) as mock_run:
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 1)
            self.assertEqual(mock_run.call_count, 3)


if __name__ == "__main__":
    unittest.main()
