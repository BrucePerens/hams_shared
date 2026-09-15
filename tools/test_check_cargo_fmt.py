#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_cargo_fmt.py.

subprocess.run is mocked throughout, the same boundary-mocking approach test_check_cargo_clippy.py
uses. Crate discovery is check_cargo_clippy.find_cargo_crates(), already tested there against real
temp-directory fixtures.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import check_cargo_fmt as chk  # noqa: E402


def _write(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _argv(self):
        return ["check_cargo_fmt.py", self.tmp]

    def _make_crate(self, name="some_daemon"):
        crate = os.path.join(self.tmp, "daemons", name)
        _write(os.path.join(crate, "Cargo.toml"), f"[package]\nname = \"{name}\"\n")
        return crate

    def _run_main(self, side_effect):
        with patch.object(sys, "argv", self._argv()), patch(
            "check_cargo_fmt.subprocess.run", side_effect=side_effect
        ) as mock_run, patch("builtins.print") as mock_print:
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
        printed = " ".join(str(c) for c in mock_print.call_args_list)
        return ctx.exception.code, mock_run, printed

    def test_no_crates_exits_0_without_calling_subprocess_at_all(self):
        code, mock_run, _ = self._run_main(side_effect=[])
        self.assertEqual(code, 0)
        mock_run.assert_not_called()

    def test_rustfmt_not_installed_exits_1_with_a_clear_message(self):
        self._make_crate()
        code, mock_run, printed = self._run_main(side_effect=[MagicMock(returncode=1)])
        self.assertEqual(code, 1)
        self.assertEqual(mock_run.call_count, 1)
        self.assertIn("not installed", printed)

    def test_a_formatted_crate_exits_0(self):
        crate = self._make_crate()
        clean = MagicMock(returncode=0, stdout="", stderr="")
        code, mock_run, _ = self._run_main(side_effect=[MagicMock(returncode=0), clean])
        self.assertEqual(code, 0)
        args, kwargs = mock_run.call_args_list[1]
        self.assertEqual(args[0], ["cargo", "fmt", "--", "--check"])
        self.assertEqual(kwargs["cwd"], crate)

    def test_an_unformatted_crate_exits_1_and_prints_the_diff(self):
        self._make_crate()
        diff = MagicMock(returncode=1, stdout="Diff in /x/src/main.rs:777:\n-a\n+b\n", stderr="")
        code, _, printed = self._run_main(side_effect=[MagicMock(returncode=0), diff])
        self.assertEqual(code, 1)
        self.assertIn("Diff in /x/src/main.rs:777", printed)
        self.assertIn(os.path.join("daemons", "some_daemon"), printed)

    def test_every_crate_is_checked_even_after_a_finding(self):
        self._make_crate("a_daemon")
        self._make_crate("b_daemon")
        diff = MagicMock(returncode=1, stdout="Diff in lib.rs:1:\n", stderr="")
        clean = MagicMock(returncode=0, stdout="", stderr="")
        code, mock_run, _ = self._run_main(side_effect=[MagicMock(returncode=0), diff, clean])
        self.assertEqual(code, 1)
        self.assertEqual(mock_run.call_count, 3)

    def test_the_gate_never_rewrites_files(self):
        """run_linters.py runs in the working tree other sessions share. A plain `cargo fmt` there
        would reformat their uncommitted edits, so every per-crate call must carry --check."""
        self._make_crate("a_daemon")
        self._make_crate("b_daemon")
        clean = MagicMock(returncode=0, stdout="", stderr="")
        _, mock_run, _ = self._run_main(side_effect=[MagicMock(returncode=0), clean, clean])
        for args, _kwargs in mock_run.call_args_list[1:]:
            self.assertEqual(args[0][:2], ["cargo", "fmt"])
            self.assertIn("--check", args[0])


if __name__ == "__main__":
    unittest.main()
