#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_cargo_deny.py.

main()'s actual `cargo deny check` invocation is a real, potentially slow subprocess call --
never exercised for real here. subprocess.run is mocked throughout main()'s tests, the same
boundary-mocking approach test_check_pip_audit.py uses for its own subprocess calls.
find_deny_crates() has no subprocess involvement at all and is tested directly against real
temp-directory fixtures.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_cargo_deny as chk  # noqa: E402


def _write(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class FindDenyCratesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_crate_with_both_cargo_toml_and_deny_toml_is_found(self):
        crate = os.path.join(self.tmp, "daemons", "some_daemon")
        _write(os.path.join(crate, "Cargo.toml"))
        _write(os.path.join(crate, "deny.toml"))
        self.assertEqual(chk.find_deny_crates(self.tmp), [crate])

    def test_a_crate_with_only_cargo_toml_is_not_found(self):
        crate = os.path.join(self.tmp, "daemons", "no_deny_toml")
        _write(os.path.join(crate, "Cargo.toml"))
        self.assertEqual(chk.find_deny_crates(self.tmp), [])

    def test_an_ignored_directory_is_never_walked(self):
        crate = os.path.join(self.tmp, "node_modules", "pkg")
        _write(os.path.join(crate, "Cargo.toml"))
        _write(os.path.join(crate, "deny.toml"))
        self.assertEqual(chk.find_deny_crates(self.tmp), [])

    def test_multiple_crates_are_all_found_and_sorted(self):
        crate_a = os.path.join(self.tmp, "daemons", "a_daemon")
        crate_b = os.path.join(self.tmp, "daemons", "b_daemon")
        for crate in (crate_a, crate_b):
            _write(os.path.join(crate, "Cargo.toml"))
            _write(os.path.join(crate, "deny.toml"))
        found = chk.find_deny_crates(self.tmp)
        self.assertEqual(len(found), 2)
        self.assertEqual(found, sorted(found))


class MainTests(unittest.TestCase):
    """subprocess.run is mocked throughout -- no real `cargo deny check` invocation is ever made
    in these tests."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _argv(self):
        return ["check_cargo_deny.py", self.tmp]

    def _make_crate(self):
        crate = os.path.join(self.tmp, "daemons", "some_daemon")
        _write(os.path.join(crate, "Cargo.toml"))
        _write(os.path.join(crate, "deny.toml"))
        return crate

    def test_no_deny_crates_exits_0_without_calling_subprocess_at_all(self):
        with patch.object(sys, "argv", self._argv()), patch(
            "check_cargo_deny.subprocess.run"
        ) as mock_run:
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 0)
            mock_run.assert_not_called()

    def test_cargo_deny_not_installed_exits_1_with_a_clear_message(self):
        self._make_crate()
        version_check = MagicMock(returncode=1)
        with patch.object(sys, "argv", self._argv()), patch(
            "check_cargo_deny.subprocess.run", return_value=version_check
        ) as mock_run, patch("builtins.print") as mock_print:
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 1)
            self.assertEqual(mock_run.call_count, 1)
            printed = " ".join(str(c) for c in mock_print.call_args_list)
            self.assertIn("not installed", printed)

    def test_a_clean_check_exits_0(self):
        self._make_crate()
        version_check = MagicMock(returncode=0)
        deny_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(sys, "argv", self._argv()), patch(
            "check_cargo_deny.subprocess.run", side_effect=[version_check, deny_result]
        ):
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 0)

    def test_a_finding_exits_1(self):
        self._make_crate()
        version_check = MagicMock(returncode=0)
        deny_result = MagicMock(returncode=1, stdout="advisories FAILED", stderr="")
        with patch.object(sys, "argv", self._argv()), patch(
            "check_cargo_deny.subprocess.run", side_effect=[version_check, deny_result]
        ):
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 1)

    def test_multiple_crates_are_all_checked_even_after_a_finding(self):
        crate_a = os.path.join(self.tmp, "daemons", "a_daemon")
        crate_b = os.path.join(self.tmp, "daemons", "b_daemon")
        for crate in (crate_a, crate_b):
            _write(os.path.join(crate, "Cargo.toml"))
            _write(os.path.join(crate, "deny.toml"))
        version_check = MagicMock(returncode=0)
        finding = MagicMock(returncode=1, stdout="advisories FAILED", stderr="")
        clean = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(sys, "argv", self._argv()), patch(
            "check_cargo_deny.subprocess.run",
            side_effect=[version_check, finding, clean],
        ) as mock_run:
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            self.assertEqual(ctx.exception.code, 1)
            # version check + one `cargo deny check` per crate = 3 calls.
            self.assertEqual(mock_run.call_count, 3)


class ResolveRepoRootTests(unittest.TestCase):
    # This checker was added 2026-08-26, after the original dir_path-repo-root audit
    # (LINTER_POLICY_REVISIT.md) that fixed 9 other checkers -- it inherited the same
    # hams_shared-redirect pattern by copying it, but was never itself covered by that audit's
    # own regression tests until now.
    def test_a_hams_shared_path_redirects_to_its_parent_repo(self):
        fake_repo = os.path.join(os.sep, "some", "workspace", "some_repo")
        self.assertEqual(
            chk._resolve_repo_root(os.path.join(fake_repo, "hams_shared")),
            fake_repo,
        )

    def test_a_real_repo_root_passes_through_unchanged(self):
        fake_repo = os.path.join(os.sep, "some", "workspace", "some_repo")
        self.assertEqual(chk._resolve_repo_root(fake_repo), fake_repo)


class ResolveRepoRootsTests(unittest.TestCase):
    # Regression test for the real gap found 2026-08-27: all four real deny.toml crates
    # (hams_local_relay, hams_relay_bridge, hams_data_relay, hams_simulated_band) live under
    # hams_com/daemons/; hams_open's own daemons/ham_digital_modes has no deny.toml at all --
    # confirmed directly, run_linters.py's own real invocation was finding ZERO deny.toml crates
    # and silently exiting 0 without ever once running `cargo deny` on any of them.
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.workspace = os.path.join(self.tmp, "workspace")
        os.makedirs(self.workspace)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_repo(self, name, module_names=()):
        repo = os.path.join(self.workspace, name)
        for module_name in module_names:
            _write(os.path.join(repo, module_name, "__manifest__.py"), "{}")
        return repo

    def test_hams_shared_input_appends_the_real_hams_com_sibling(self):
        self._make_repo("hams_open", module_names=["zero_sudo"])
        hams_com = self._make_repo("hams_com", module_names=["ham_base"])
        hams_shared = os.path.join(self.workspace, "hams_open", "hams_shared")
        os.makedirs(hams_shared)
        roots = chk._resolve_repo_roots(hams_shared)
        self.assertEqual(roots, [os.path.join(self.workspace, "hams_open"), hams_com])

    def test_a_real_repo_root_with_no_odoo_sibling_present_scans_alone(self):
        repo = self._make_repo("hams_open", module_names=["zero_sudo"])
        self.assertEqual(chk._resolve_repo_roots(repo), [repo])

    def test_a_sibling_directory_with_no_manifest_py_anywhere_is_not_treated_as_a_repo(self):
        repo = self._make_repo("hams_open", module_names=["zero_sudo"])
        os.makedirs(os.path.join(self.workspace, "hams_com", "not_a_module"))
        self.assertEqual(chk._resolve_repo_roots(repo), [repo])


if __name__ == "__main__":
    unittest.main()
