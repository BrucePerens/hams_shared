#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_pip_audit.py.

main()'s actual pip-audit invocation makes a real network call against
PyPI's advisory database -- never exercised for real here. subprocess.run
is mocked throughout main()'s tests so no test in this file makes any
network call, the same boundary-mocking approach used for
check_bot_compliance.py's DNS/HTTP calls in this same sweep.
find_requirements_files() has no subprocess involvement at all and is
tested directly against real temp-directory fixtures.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_pip_audit as chk  # noqa: E402


def _write(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class FindRequirementsFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_root_level_requirements_txt_is_found(self):
        _write(os.path.join(self.tmp, "requirements.txt"))
        self.assertEqual(
            chk.find_requirements_files(self.tmp),
            [os.path.join(self.tmp, "requirements.txt")],
        )

    def test_a_variant_name_like_requirements_dev_txt_is_found(self):
        _write(os.path.join(self.tmp, "requirements-dev.txt"))
        found = chk.find_requirements_files(self.tmp)
        self.assertEqual(len(found), 1)

    def test_a_different_extension_is_not_found(self):
        _write(os.path.join(self.tmp, "requirements.in"))
        self.assertEqual(chk.find_requirements_files(self.tmp), [])

    def test_a_file_not_starting_with_requirements_is_not_found(self):
        _write(os.path.join(self.tmp, "dev_requirements.txt"))
        self.assertEqual(chk.find_requirements_files(self.tmp), [])

    def test_an_ignored_directory_is_never_walked(self):
        _write(os.path.join(self.tmp, "node_modules", "pkg", "requirements.txt"))
        self.assertEqual(chk.find_requirements_files(self.tmp), [])

    def test_multiple_files_across_directories_are_all_found_and_sorted(self):
        _write(os.path.join(self.tmp, "daemons", "foo", "requirements.txt"))
        _write(os.path.join(self.tmp, "ingest", "requirements.txt"))
        found = chk.find_requirements_files(self.tmp)
        self.assertEqual(len(found), 2)
        self.assertEqual(found, sorted(found))


class MainTests(unittest.TestCase):
    """subprocess.run is mocked throughout -- no real pip-audit invocation
    (and therefore no real network call) is ever made in these tests."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _argv(self):
        return ["check_pip_audit.py", self.tmp]

    def test_no_requirements_files_exits_0_without_calling_subprocess_at_all(self):
        with patch.object(sys, "argv", self._argv()), patch(
            "check_pip_audit.subprocess.run"
        ) as mock_run:
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 0)
            mock_run.assert_not_called()

    def test_pip_audit_not_installed_exits_1_with_a_clear_message(self):
        _write(os.path.join(self.tmp, "requirements.txt"), "requests==2.0\n")
        version_check = MagicMock(returncode=1)
        with patch.object(sys, "argv", self._argv()), patch(
            "check_pip_audit.subprocess.run", return_value=version_check
        ) as mock_run, patch("builtins.print") as mock_print:
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 1)
            # Only the --version check ran; the -r audit invocation never
            # should have been attempted once the install check failed.
            self.assertEqual(mock_run.call_count, 1)
            printed = " ".join(str(c) for c in mock_print.call_args_list)
            self.assertIn("not installed", printed)

    def test_a_clean_audit_result_exits_0(self):
        _write(os.path.join(self.tmp, "requirements.txt"), "requests==2.0\n")
        version_check = MagicMock(returncode=0)
        audit_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(sys, "argv", self._argv()), patch(
            "check_pip_audit.subprocess.run", side_effect=[version_check, audit_result]
        ):
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 0)

    def test_a_finding_in_the_audit_exits_1(self):
        _write(os.path.join(self.tmp, "requirements.txt"), "requests==2.0\n")
        version_check = MagicMock(returncode=0)
        audit_result = MagicMock(returncode=1, stdout="VULNERABILITY FOUND", stderr="")
        with patch.object(sys, "argv", self._argv()), patch(
            "check_pip_audit.subprocess.run", side_effect=[version_check, audit_result]
        ):
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 1)

    def test_multiple_requirements_files_are_all_audited_even_after_a_finding(self):
        _write(os.path.join(self.tmp, "requirements.txt"), "requests==2.0\n")
        _write(os.path.join(self.tmp, "requirements-dev.txt"), "pytest==8.0\n")
        version_check = MagicMock(returncode=0)
        finding = MagicMock(returncode=1, stdout="VULN", stderr="")
        clean = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(sys, "argv", self._argv()), patch(
            "check_pip_audit.subprocess.run", side_effect=[version_check, finding, clean]
        ) as mock_run:
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 1)
            # 1 version check + 2 per-file audits, not stopped early after
            # the first file's finding.
            self.assertEqual(mock_run.call_count, 3)


if __name__ == "__main__":
    unittest.main()
